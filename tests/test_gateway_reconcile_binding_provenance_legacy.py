"""Binding provenance (#1850 round 4): illegal shapes, explicit provenance and legacy v1 rows.

Partition of ``tests/test_gateway_reconcile_binding_provenance.py`` (#3177):
non-bind commit shapes are refused with zero writes, the normalizer rejects
explicit provenance on non-accepted outcomes and mismatched matched ids, and
legacy v1 bound rows replay idempotently only on the same lane. The shared
field names are in ``tests/gateway_reconcile_binding_provenance_helpers.py``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from tests.gateway_reconcile_binding_provenance_helpers import (
    _ANCHOR,
    _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    _SLURM_BINDING_SOURCE_FIELD,
)
from tests.gateway_reconcile_helpers import (
    _commit_name_window_fallback_bind,
    _file_cohort_repository,
    _versioned_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

# ---------------------------------------------------------------------------
# #1850 round 4: typed-boundary closure.  Fix A refuses non-bind commit
# shapes; Fix B closes the durable normalizer on bound identity; Fix C keeps
# legacy v1 replays idempotent without minting provenance.
# ---------------------------------------------------------------------------


def _commit_illegal_bind_shape_rejected(
    repository: Any,
    key: str,
    pid: str,
    *,
    decision: str,
    status: str,
    reason_class: str | None = None,
    identity_blocked_streak: int = 0,
) -> None:
    """Drive the typed commit with an ``accepted`` transition that carries an
    accounting decision but is NOT one of the three legal bind lanes. Such a
    transition derives ``binding_source=None`` (Fix A): the commit must refuse
    it BEFORE any mutation or occupancy scan, leaving the reservation and the
    journal zero-changed. The transition itself is construction-legal (an
    accepted-submit with a valid accounting decision/status/reason tuple), so
    this is genuinely a commit-API boundary, not a transition-class refusal."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    transition = AcceptedSubmitTransition.accounting(
        decision,
        submit_outcome="accepted",
        reconciliation_reason_class=reason_class,
        status=status,
        identity_blocked_streak=identity_blocked_streak,
    )
    journal_path = repository.root / "journal" / "gfs" / "2026071200.jsonl"
    before_bytes = journal_path.read_bytes() if journal_path.exists() else b""
    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=transition,
        )
    assert error.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert error.value.field == "transition"
    assert not repository.get_pipeline_job(pid)["slurm_job_id"], (
        "an illegal bind shape must never bind the numeric id"
    )
    assert repository.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert repository.get_pipeline_job(pid)[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] is None
    after_bytes = journal_path.read_bytes() if journal_path.exists() else b""
    assert after_bytes == before_bytes, "an illegal bind shape must not write journal bytes"
    reopened = FileOrchestrationJournalRepository(repository.root)
    assert reopened.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] is None
    assert reopened.get_pipeline_job(pid)["slurm_job_id"] is None


def test_illegal_accepted_accounting_decision_commits_all_rejected_zero_write(
    tmp_path: Any,
) -> None:
    """Fix A: the typed commit accepts ONLY the three legal bind shapes. An
    ``accepted`` transition carrying a held/defer/blocked accounting decision
    (accounting_unavailable, identity_mismatch_blocked, absence_deferred) is not
    a bind -- it must be refused with the stable authority error, never bind the
    numeric id, never mint ``binding_source=None`` provenance, and leave the
    journal bytes zero-changed (no mutation, no occupancy scan)."""

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        pid = str(repository.query_candidate_state(key)["job_id"])
        for decision, status, reason_class in (
            ("accounting_unavailable", "submitted", "comment_accounting_unproven"),
            ("identity_mismatch_blocked", "submitted", None),
            ("absence_deferred", "submitted", None),
        ):
            _commit_illegal_bind_shape_rejected(
                repository,
                key,
                pid,
                decision=decision,
                status=status,
                reason_class=reason_class,
            )


def test_legal_three_bind_lanes_still_commit_after_illegal_shape_refused(
    tmp_path: Any,
) -> None:
    """The three legal bind lanes (ordinary gateway, exact-comment matched,
    name-window matched fallback) still commit and mint their derived binding
    provenance after an illegal accepted accounting-decision shape was refused
    on the same reservation (fail-closed refusal does not wedge the attempt)."""

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
    )

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "gfs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        key = "cycle_gfs_2026071200_forecast_fixture:forecast"
        pid = str(repository.query_candidate_state(key)["job_id"])
        # Illegal shape first: refused, reservation untouched.
        with pytest.raises(FileOrchestrationJournalError):
            repository.commit_pipeline_job_submit_attempt(
                key,
                pipeline_job_id=pid,
                expected_submission_attempt=1,
                slurm_job_id="72001",
                submitted_at=_ANCHOR + timedelta(minutes=30),
                transition=AcceptedSubmitTransition.accounting(
                    "accounting_unavailable",
                    submit_outcome="accepted",
                    reconciliation_reason_class="comment_accounting_unproven",
                    status="submitted",
                ),
            )
        assert repository.get_pipeline_job(pid)["slurm_job_id"] is None

        # Ordinary gateway lane: applied, gateway provenance.
        ordinary = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=pid,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert ordinary.outcome == "applied"
        assert repository.get_pipeline_job(pid)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"

        # Exact-comment matched lane on a fresh row (separate root so the
        # shared journal root stays single-owner per test).
        repository2 = _file_cohort_repository(
            tmp_path / "ifs",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="ifs",
        )
        key2 = "cycle_ifs_2026071200_forecast_fixture:forecast"
        pid2 = str(repository2.query_candidate_state(key2)["job_id"])
        exact = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=pid2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert exact.outcome == "applied"
        assert repository2.get_pipeline_job(pid2)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_exact_comment"

        # Name-window matched fallback lane (separate root again).
        repository3 = _file_cohort_repository(
            tmp_path / "era5",
            created_at=_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            source_id="era5",
        )
        key3 = "cycle_era5_2026071200_forecast_fixture:forecast"
        pid3 = str(repository3.query_candidate_state(key3)["job_id"])
        fallback = _commit_name_window_fallback_bind(
            repository3, key3, slurm_job_id="72001", canonical_submit=_ANCHOR + timedelta(hours=1)
        )
        assert fallback.committed
        assert repository3.get_pipeline_job(pid3)[_SLURM_BINDING_SOURCE_FIELD()] == "slurm_name_window_unique"
        assert repository3.get_pipeline_job(pid3)[
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()
        ] == "2026-07-12T01:00:00Z"


def test_normalizer_rejects_explicit_provenance_on_non_accepted_outcomes() -> None:
    """Fix B: explicit binding provenance is legal ONLY on an ``accepted``
    submit outcome. A durable row that is ``rejected`` or
    ``submit_result_ambiguous`` yet carries a numeric bound id AND explicit
    provenance would be a bind-shaped non-bind -- corrupt evidence, rejected
    by the durable normalizer (closed world on bound identity)."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    base = {
        **_versioned_master_reservation_record(member_count=1),
        "status": "submitted",
        "slurm_job_id": "72001",
    }
    for outcome, decision, source in (
        ("rejected", None, "gateway_submit"),
        ("submit_result_ambiguous", None, "gateway_submit"),
        ("rejected", "matched_bound", "slurm_exact_comment"),
        ("submit_result_ambiguous", "matched_bound", "slurm_exact_comment"),
    ):
        row = dict(base)
        row["submit_outcome"] = outcome
        if decision is None:
            row["reconciliation_source"] = None
            row["reconciliation_decision"] = None
            row["matched_slurm_job_id"] = None
            row[_SLURM_BINDING_SOURCE_FIELD()] = source
            row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = None
        else:
            row["reconciliation_source"] = source
            row["reconciliation_decision"] = decision
            row["matched_slurm_job_id"] = "72001"
            row[_SLURM_BINDING_SOURCE_FIELD()] = "slurm_exact_comment"
            row[_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD()] = None
        with pytest.raises(AcceptedSubmitEvidenceError) as error:
            normalize_accepted_submit_evidence(row)
        assert error.value.field == "submit_outcome", f"{outcome}/{decision}"


def test_normalizer_rejects_matched_bound_mismatched_matched_id() -> None:
    """Fix B: a ``matched_bound`` row must be internally consistent -- its
    matched Slurm id must equal the bound numeric ``slurm_job_id``. A matched
    id that contradicts the owned id is corrupt evidence and must be rejected
    even when the source/provenance pairing is otherwise legal."""

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    # exact-comment matched row, matched id contradicts bound id.
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72002",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
            }
        )
    assert error.value.field == "matched_slurm_job_id"

    # name-window matched row, matched id contradicts bound id (even with the
    # canonical Submit present).
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "submitted",
                "submit_outcome": "accepted",
                "slurm_job_id": "72001",
                "reconciliation_source": "slurm_name_window_unique",
                "reconciliation_decision": "matched_bound",
                "matched_slurm_job_id": "72002",
                _SLURM_BINDING_SOURCE_FIELD(): "slurm_name_window_unique",
                _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): "2026-07-12T01:00:00Z",
            }
        )
    assert error.value.field == "matched_slurm_job_id"

    # A legal matched-bound row with consistent ids still normalizes.
    normalized = normalize_accepted_submit_evidence(
        {
            **_versioned_master_reservation_record(member_count=1),
            "status": "submitted",
            "submit_outcome": "accepted",
            "slurm_job_id": "72001",
            "reconciliation_source": "slurm_exact_comment",
            "reconciliation_decision": "matched_bound",
            "matched_slurm_job_id": "72001",
            _SLURM_BINDING_SOURCE_FIELD(): "slurm_exact_comment",
            _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(): None,
        }
    )
    assert normalized["matched_slurm_job_id"] == "72001"


def _legacy_v1_bound_row(
    *,
    source: str,
    decision: str | None,
    matched: str | None,
    slurm_job_id: str,
    source_id: str = "gfs",
) -> dict[str, Any]:
    """A pre-#1850 v1 bound row: bound id and accounting tuple WITHOUT either
    additive binding-provenance field (what a v1 writer legitimately produced).
    The two provenance keys are omitted entirely, so the durable row stays
    readable and the replay must NOT backfill/mint them."""
    row = {
        **_versioned_master_reservation_record(member_count=1, source_id=source_id),
        "status": "succeeded",
        "submit_outcome": "accepted",
        "reconciliation_source": source,
        "reconciliation_decision": decision,
        "matched_slurm_job_id": matched,
        "slurm_job_id": slurm_job_id,
        "submitted_at": "2026-07-12T01:00:00.123456Z",
    }
    row.pop(_SLURM_BINDING_SOURCE_FIELD(), None)
    row.pop(_SLURM_ACCOUNTING_SUBMITTED_AT_FIELD(), None)
    return row


def _write_master_row(repository: Any, row: dict[str, Any]) -> None:
    """Append a raw v1-style master row into the journal (the authoritative
    surface the typed commit replays), then assert it decodes as a current
    bound master. A legacy v1 writer produced exactly this shape: bound id and
    accounting tuple with NEITHER additive provenance field."""
    from services.orchestrator.accepted_submit_identity import (
        accepted_submit_contract_is_current,
        accepted_submit_row_kind,
    )
    from services.orchestrator.file_orchestration_journal import (
        _cycle_time_from_job,
        _source_id_from_job,
    )

    job_id = str(row["job_id"])
    repository._append_validated_record_unlocked(
        "pipeline_job",
        row,
        source_id=_source_id_from_job(row),
        cycle_time=_cycle_time_from_job(row),
        model_id=None,
    )
    decoded = repository._pipeline_job_for_id_unlocked(job_id)
    assert decoded is not None
    assert accepted_submit_row_kind(decoded) == "master"
    assert accepted_submit_contract_is_current(decoded)


def _snapshot_row(repository: Any, job_id: str) -> dict[str, Any]:
    """The authoritative journal-replayed master row (what the typed commit
    reads), used to prove a replay is byte-for-byte zero-write."""
    decoded = repository._pipeline_job_for_id_unlocked(str(job_id))
    assert decoded is not None
    return dict(decoded)


def test_legacy_v1_same_lane_replay_idempotent_no_backfill(tmp_path: Any) -> None:
    """Fix C: a pre-#1850 v1 row (both additive provenance fields absent)
    replayed same-lane with the same id/tuple must be ``idempotent`` and
    zero-write -- and must NOT be backfilled/minted with provenance by the
    replay (read-compatible, the replay never rewrites historical authority).
    Covered for BOTH the ordinary (gateway_submit) lane and the exact-comment
    matched lane. Cross-lane or a name-window replay on the missing-fields row
    is never idempotent."""

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

        # --- Ordinary lane: v1 row bound as gateway submit. ---
        _write_master_row(
            repository,
            _legacy_v1_bound_row(
                source=None,
                decision=None,
                matched=None,
                slurm_job_id="72001",
            ),
        )
        assert repository.get_pipeline_job(job_id)["slurm_job_id"] == "72001"
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
        before = _snapshot_row(repository, job_id)

        replay = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert replay.committed
        assert replay.outcome == "idempotent"
        assert not replay.wrote
        assert _snapshot_row(repository, job_id) == before, (
            "replay must be zero-write and must NOT backfill/mint provenance"
        )
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None

        # --- Exact-comment lane: v1 row bound with matched tuple. ---
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
        _write_master_row(
            repository2,
            _legacy_v1_bound_row(
                source="slurm_exact_comment",
                decision="matched_bound",
                matched="72001",
                slurm_job_id="72001",
                source_id="ifs",
            ),
        )
        before2 = _snapshot_row(repository2, job_id2)
        replay2 = repository2.commit_pipeline_job_submit_attempt(
            key2,
            pipeline_job_id=job_id2,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert replay2.outcome == "idempotent"
        assert not replay2.wrote
        assert _snapshot_row(repository2, job_id2) == before2


def test_legacy_v1_partial_provenance_and_contradiction_not_idempotent(
    tmp_path: Any,
) -> None:
    """Fix C edge closure: a legacy row with only ONE of the two additive
    fields present, or a replay that explicitly contradicts the durable
    provenance/canonical, must NOT be idempotent (stale, zero-write). The
    missing-both-fields compatibility applies ONLY to a fully-legacy row."""

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

        # Single-field-only legacy-ish row: binding source present, canonical
        # absent. Not a full v1 shape; never idempotent.
        partial = _legacy_v1_bound_row(
            source=None,
            decision=None,
            matched=None,
            slurm_job_id="72001",
        )
        partial[_SLURM_BINDING_SOURCE_FIELD()] = "gateway_submit"
        _write_master_row(repository, partial)
        replay = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert replay.outcome == "stale"
        assert not replay.wrote
        assert repository.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id)[_SLURM_BINDING_SOURCE_FIELD()] == "gateway_submit"


def test_legacy_v1_cross_lane_replay_not_idempotent(tmp_path: Any) -> None:
    """Fix C lane closure: a fully-legacy missing-both-fields row is idempotent
    only for its OWN legal lane (ordinary or exact-comment). A cross-lane
    replay (exact-comment matched on the legacy ordinary row) and a name-window
    replay on the missing-fields row are never idempotent -- name-window
    provenance is this PR's addition and a missing-fields row cannot be a legal
    fallback bind."""

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
        _write_master_row(
            repository,
            _legacy_v1_bound_row(
                source=None,
                decision=None,
                matched=None,
                slurm_job_id="72001",
            ),
        )

        # Cross-lane: exact-comment matched on the legacy ordinary row.
        cross = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            submitted_at=_ANCHOR + timedelta(minutes=30),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_exact_comment",
            ),
        )
        assert cross.outcome == "stale"
        assert not cross.wrote
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None

        # Name-window fallback replay on the missing-fields row: never
        # idempotent -- name-window provenance is this PR's addition and a
        # missing-fields row cannot be a legal fallback bind, so the replay
        # resolves stale, zero-write, and mints nothing.
        name_window = repository.commit_pipeline_job_submit_attempt(
            key,
            pipeline_job_id=job_id,
            expected_submission_attempt=1,
            slurm_job_id="72001",
            slurm_accounting_submitted_at=_ANCHOR + timedelta(hours=1),
            transition=AcceptedSubmitTransition.accounting(
                "matched_bound",
                submit_outcome="accepted",
                matched_slurm_job_id="72001",
                status="submitted",
                reconciliation_source="slurm_name_window_unique",
            ),
        )
        assert name_window.outcome == "stale"
        assert not name_window.wrote
        assert repository.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(job_id).get(_SLURM_BINDING_SOURCE_FIELD()) is None
