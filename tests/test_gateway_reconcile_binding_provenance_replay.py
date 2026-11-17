"""Binding provenance (#1850 round 2/3): canonical authority and replay idempotency.

Partition of ``tests/test_gateway_reconcile_binding_provenance.py`` (#3177):
legacy ``submitted_at`` never proves an incarnation, reconcile persists only the
canonical submit, same-lane replays are idempotent and cross-lane replays are
not, and a deferred held tuple later restores. The shared field names and
projection/fallback helpers are in
``tests/gateway_reconcile_binding_provenance_helpers.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

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
from tests.gateway_reconcile_helpers import (
    _commit_name_window_fallback_bind,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

# ---------------------------------------------------------------------------
# #1850 round 2 typed-boundary regressions: single canonical authority, no
# duplicate/forgeable transition provenance, legacy submitted_at never proves
# recycle or drives the scan.
# ---------------------------------------------------------------------------


def test_legacy_submitted_at_differs_from_canonical_does_not_change_incarnation_scan(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimant/occupancy scan uses ONLY the canonical accounting Submit.

    A fallback commit with legacy ``submitted_at`` (B) differing from the
    canonical (C) must still adjudicate incarnation by C: a same-C sibling
    blocks, a different-C sibling recycles, and the durable canonical equals C
    (never B). Legacy ``submitted_at`` is stored as the gateway/commit time
    only."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
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
        current = repository.query_candidate_state(key)
        # B=01:00 legacy, C=03:00 canonical: the mismatch must NOT change
        # incarnation adjudication.
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=3),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert bind.committed
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T03:00:00Z"
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        # Legacy submitted_at is the stored gateway/commit time (B), not C.
        assert bound["submitted_at"] == "2026-07-12T01:00:00Z"

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

        # GFS candidate Submit 03:00 (== C): same incarnation blocks even
        # though legacy B differs.
        gfs_job_id = _reserve_gfs_sibling(repository, created_at=_ANCHOR + timedelta(hours=1))
        query_end = _ANCHOR + timedelta(hours=4)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T03:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == gfs_job_id
        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None

        # GFS candidate Submit 04:00 (different canonical): legitimate recycle.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs2",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        current2 = repository2.query_candidate_state(key)
        bind2 = repository2.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current2["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(hours=1),
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=3),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
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
            rows=_fallback_row("72001", submit="2026-07-12T04:00:00"),
            query_end=query_end,
        )
        outcome2 = reconcile_reserved_unbound_jobs(
            repository2, comment_query=query2, now=lambda: query_end
        )[0]
        assert outcome2.job_id == gfs_job_id2
        assert outcome2.action == "bound"
        assert outcome2.matched_slurm_job_id == "72001"
        assert repository2.get_pipeline_job(gfs_job_id2)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T04:00:00Z"
        reopened2 = FileOrchestrationJournalRepository(repository2.root)
        assert reopened2.get_pipeline_job(job_id)["slurm_job_id"] == "72001"


def test_reconcile_fallback_persists_only_canonical_submit_never_legacy_duplicate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end reconcile fallback writes the parsed sacct Submit into the
    canonical field only; the legacy submitted_at is the commit-now default and
    never equals/pretends to be the sacct Submit."""

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        query_end = _ANCHOR + timedelta(hours=2)
        query = _fallback_querier(
            monkeypatch,
            rows=_fallback_row("72001", submit="2026-07-12T01:00:00"),
            query_end=query_end,
        )
        outcome = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: query_end
        )[0]
        assert outcome.job_id == job_id
        assert outcome.action == "bound"
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        # The legacy submitted_at is NOT the sacct Submit: the reconcile path
        # does not pass it, so it falls back to the commit-now default which is
        # AFTER the query-end (the fallback Submit), i.e. clearly distinct.
        assert bound["submitted_at"] != "2026-07-12T01:00:00Z"
        # Reopened journal agrees.
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"


def test_ordinary_accepted_commit_writes_gateway_provenance(tmp_path: Any) -> None:
    """A legal ordinary accepted commit (decision None, no canonical input)
    records gateway_submit binding provenance and no canonical accounting
    Submit."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

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
        current = repository.query_candidate_state(key)
        bind = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=str(current["job_id"]),
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert bind.committed
        bound = repository.get_pipeline_job(job_id)
        assert bound[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        assert bound[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
        # The live gateway submit carries no accounting reconciliation tuple.
        assert bound["reconciliation_source"] is None
        assert bound["reconciliation_decision"] is None
        assert bound["submit_outcome"] == "accepted"


# ---------------------------------------------------------------------------
# #1850 round 3: conflicting idempotent replay and durable matched-bound
# source/provenance consistency (single canonical authority).
# ---------------------------------------------------------------------------


def test_conflicting_fallback_replay_is_noncommitted_and_durable_unchanged(
    tmp_path: Any,
) -> None:
    """Same reservation/attempt/id/tuple replayed with a DIFFERENT canonical
    Submit must be non-committed (``stale``), zero-write, and leave the durable
    canonical unchanged. Only the exact same (id, canonical Submit, source)
    replay may be ``idempotent``."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
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
        current = repository.query_candidate_state(key)
        pid = str(current["job_id"])

        def fallback(canonical: datetime) -> Any:
            return repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=pid,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                slurm_accounting_submitted_at=canonical,
                transition=AcceptedSubmitTransition.accounting(
                    "matched_bound",
                    submit_outcome="accepted",
                    matched_slurm_job_id="72001",
                    status="submitted",
                    reconciliation_source="slurm_name_window_unique",
                ),
            )

        first = fallback(_ANCHOR + timedelta(hours=1))
        assert first.outcome == "applied"
        assert first.wrote
        assert repository.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"

        # Conflicting replay: same tuple, different canonical Submit.
        conflicting = fallback(_ANCHOR + timedelta(hours=2))
        assert not conflicting.committed
        assert not conflicting.wrote
        assert conflicting.outcome == "stale"
        assert repository.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"

        # Exact same replay: idempotent, zero-write.
        same = fallback(_ANCHOR + timedelta(hours=1))
        assert same.committed
        assert not same.wrote
        assert same.outcome == "idempotent"


def test_ordinary_and_exact_comment_same_lane_replays_are_idempotent_and_cross_lane_is_not(
    tmp_path: Any,
) -> None:
    """Ordinary and exact-comment same-lane replays stay idempotent; a
    cross-lane replay of the same id/tuple is never mistaken for idempotent."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
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
        current = repository.query_candidate_state(key)
        pid = str(current["job_id"])

        ordinary = AcceptedSubmitTransition.accepted(status="submitted")
        exact = AcceptedSubmitTransition.accounting(
            "matched_bound",
            submit_outcome="accepted",
            matched_slurm_job_id="72001",
            status="submitted",
            reconciliation_source="slurm_exact_comment",
        )

        first = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=ordinary,
        )
        assert first.outcome == "applied"
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Same ordinary lane replay: idempotent.
        same = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=ordinary,
        )
        assert same.outcome == "idempotent"
        assert not same.wrote

        # Cross-lane replay: exact-comment matched on the gateway-bound row is
        # NOT idempotent (different bind lane / derived binding source).
        cross = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert not cross.committed
        assert not cross.wrote
        assert cross.outcome == "stale"
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Exact-comment lane on a fresh row: applied then same-lane idempotent.
        repository2 = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        job_id2 = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
        key2 = "cycle_ifs_2026071200_forecast_fixture:forecast"
        current2 = repository2.query_candidate_state(key2)
        pid2 = str(current2["job_id"])
        applied2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert applied2.outcome == "applied"
        assert repository2.get_pipeline_job(job_id2)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"
        replay2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=exact,
        )
        assert replay2.outcome == "idempotent"
        assert not replay2.wrote


def test_matched_bound_source_provenance_mismatch_rows_rejected() -> None:
    """A durable matched_bound row whose current reconciliation source
    contradicts its immutable binding provenance is corrupt evidence (design
    D3): the current source must equal the legal restoration from binding
    provenance."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    # name-window current source + gateway binding source -> reject
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "gateway_submit",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
            }
        )
    assert error.value.field == "reconciliation_source"

    # exact-comment current source + name-window binding + canonical -> reject
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72001",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == "reconciliation_source"


def test_fallback_defer_held_tuple_remains_readable_and_later_restores(
    tmp_path: Any,
) -> None:
    """A fallback-bound row parked at accounting_unavailable keeps current
    source exact-comment with immutable name-window binding provenance; the
    later matched_bound projection restores the name-window current source."""

    from services.orchestrator.accepted_submit_identity import (
        normalize_accepted_submit_evidence,
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

        # Defer (incomplete coverage) parks the row on the held tuple.
        projections, _complete = _incomplete_projection(repository, job_id)
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
        assert deferred["reconciliation_source"] == "slurm_exact_comment"
        assert deferred[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert deferred[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
        # The held tuple is readable by the durable normalizer.
        normalized = normalize_accepted_submit_evidence(deferred)
        assert normalized["reconciliation_source"] == "slurm_exact_comment"
        assert normalized[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"

        # Later complete projection restores the name-window current source.
        full, _complete = _single_projection(repository, job_id)
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
        assert terminal["reconciliation_source"] == "slurm_name_window_unique"
        assert terminal[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert terminal[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] == "2026-07-12T01:00:00Z"
