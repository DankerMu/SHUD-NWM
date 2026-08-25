"""#1180 streak / identity-release invariants on the normalization and
durable write paths.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.gateway_reconcile_helpers import (
    _file_cohort_repository,
    _versioned_master_reservation_record,
)

# ---------------------------------------------------------------------------
# #1180: the streak / identity-release invariants are the only guard on the
# ``identity_mismatch_released`` terminal semantics, and they need oracles of
# their own — a guard nobody tests is a guard a refactor silently deletes.
# ---------------------------------------------------------------------------


_INVARIANT_JOB_ID = "job_cycle_gfs_2026071200_forecast_fixture_forecast"


def _accepted_submit_invariant_fixture(tmp_path: Any, name: str) -> tuple[Any, dict[str, Any], Any]:
    """One persisted versioned master plus its durable payload, for zero-write."""

    from tests.test_file_orchestration_journal import _durable_pipeline_job_payloads

    repository = _file_cohort_repository(tmp_path / name, member_count=1)
    public = repository.get_pipeline_job(_INVARIANT_JOB_ID)
    assert public["status"] == "reserved"
    assert public["submit_outcome"] == "submit_result_ambiguous"
    durable = _durable_pipeline_job_payloads(tmp_path / name / "journal", _INVARIANT_JOB_ID)[-1]
    return repository, public, durable


def _assert_invariant_left_no_trace(
    repository: Any,
    tmp_path: Any,
    name: str,
    public: dict[str, Any],
    durable: Any,
) -> None:
    from tests.test_file_orchestration_journal import _durable_pipeline_job_payloads

    assert repository.get_pipeline_job(_INVARIANT_JOB_ID) == public
    assert _durable_pipeline_job_payloads(tmp_path / name / "journal", _INVARIANT_JOB_ID)[-1] == (
        durable
    )


@pytest.mark.parametrize(
    "streak",
    [
        pytest.param(-1, id="negative"),
        pytest.param(1.0, id="float"),
        pytest.param(True, id="bool"),
        pytest.param("1", id="str"),
    ],
)
def test_transition_rejects_a_streak_that_is_not_a_non_negative_int(
    tmp_path: Any,
    streak: Any,
) -> None:
    """#1180 J1: the counter's type gate (``bool`` included) rejects at construction."""

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository, public, durable = _accepted_submit_invariant_fixture(tmp_path, "streak-type")

    with pytest.raises(ValueError, match="identity blocked streak must be a non-negative integer"):
        repository.transition_pipeline_job_submit_evidence(
            _INVARIANT_JOB_ID,
            AcceptedSubmitTransition.accounting(
                "identity_mismatch_blocked",
                submit_outcome="submit_result_ambiguous",
                status="reserved",
                identity_blocked_streak=streak,
            ),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )

    _assert_invariant_left_no_trace(repository, tmp_path, "streak-type", public, durable)


def test_pre_outcome_transition_cannot_carry_a_streak(tmp_path: Any) -> None:
    """#1180 J2: a new reserved attempt starts clean — no inherited counter."""

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository, public, durable = _accepted_submit_invariant_fixture(tmp_path, "pre-outcome")

    with pytest.raises(ValueError, match="pre-outcome transition must begin one reserved attempt"):
        repository.transition_pipeline_job_submit_evidence(
            _INVARIANT_JOB_ID,
            AcceptedSubmitTransition(None, status="reserved", identity_blocked_streak=1),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )

    _assert_invariant_left_no_trace(repository, tmp_path, "pre-outcome", public, durable)


def test_identity_released_transition_must_abandon_the_reservation(tmp_path: Any) -> None:
    """#1180 J3: the release decision without ``reservation_lost`` is a lie.

    This is the guard that keeps ``identity_mismatch_released`` meaning
    "the reservation is gone"; recorded on a still-``reserved`` row it would
    claim a convergence exit that never happened.
    """

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository, public, durable = _accepted_submit_invariant_fixture(tmp_path, "released-status")

    with pytest.raises(ValueError, match="identity released transition must abandon the reservation"):
        repository.transition_pipeline_job_submit_evidence(
            _INVARIANT_JOB_ID,
            AcceptedSubmitTransition.accounting(
                "identity_mismatch_released",
                submit_outcome="submit_result_ambiguous",
                status="reserved",
                identity_blocked_streak=3,
            ),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )

    _assert_invariant_left_no_trace(repository, tmp_path, "released-status", public, durable)


def test_non_identity_decision_cannot_carry_a_streak(tmp_path: Any) -> None:
    """#1180 J4: the counter belongs to identity-mismatch transitions only."""

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    repository, public, durable = _accepted_submit_invariant_fixture(tmp_path, "foreign-decision")

    with pytest.raises(
        ValueError, match="identity blocked streak belongs to identity-mismatch transitions"
    ):
        repository.transition_pipeline_job_submit_evidence(
            _INVARIANT_JOB_ID,
            AcceptedSubmitTransition.accounting(
                "absence_retry_permitted",
                submit_outcome="submit_result_ambiguous",
                status="reservation_lost",
                identity_blocked_streak=1,
            ),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_statuses=("reserved",),
            require_unbound=True,
        )

    _assert_invariant_left_no_trace(repository, tmp_path, "foreign-decision", public, durable)


@pytest.mark.parametrize(
    ("mutation", "expected_reason", "expected_field", "leg"),
    [
        pytest.param(
            {"identity_blocked_streak": -1},
            "file_journal_evidence_type_invalid",
            "identity_blocked_streak",
            "J5",
            id="J5_streak_type",
        ),
        # J7/J8: these two legs pin the durable path's typed refusal, but their
        # discriminating power over their OWN guard is a fixture accident. The
        # persisted master here is decision-free and source-free, so each write
        # also diverges on a field the #1183 ordinary-upsert freeze table
        # (:1747-1754) reaches earlier; rebuild the same scenario on a persisted
        # row that already carries a decision/source and the freeze fallback
        # raises the identical (reason, field). One natural fixture edit turns
        # them into silent no-ops. The isolation claim for
        # ``accepted_submit_identity.py:646-649`` / ``:650-653`` is therefore
        # carried by the two direct-call legs below, not by these.
        pytest.param(
            {
                "reconciliation_decision": "identity_mismatch_released",
                "reconciliation_source": "slurm_exact_comment",
                "identity_blocked_streak": 3,
            },
            "file_journal_evidence_invariant_invalid",
            "reconciliation_decision",
            "J7",
            id="J7_released_while_still_reserved",
        ),
        pytest.param(
            {
                "reconciliation_decision": "absence_retry_permitted",
                "reconciliation_source": "slurm_exact_comment",
                "identity_blocked_streak": 1,
            },
            "file_journal_evidence_invariant_invalid",
            "identity_blocked_streak",
            "J8",
            id="J8_streak_on_a_foreign_decision",
        ),
    ],
)
def test_normalization_invariants_reject_the_ordinary_upsert_path(
    tmp_path: Any,
    mutation: dict[str, Any],
    expected_reason: str,
    expected_field: str,
    leg: str,
) -> None:
    """#1180 J5/J7/J8: three of the guards on the durable write path.

    ``upsert_pipeline_job`` against an already-persisted versioned master keeps
    the "left no trace" half of the assertion a real claim about durable state
    rather than a no-op around a direct function call — but see the caveat above
    the J7/J8 params, and note that the byte-identical half is itself vacuous at
    this entry point (``file_orchestration_journal.py:1757`` returns the existing
    row unconditionally for a contract-current structural master).

    Only J5 isolates its own guard here: it asserts
    ``file_journal_evidence_type_invalid``, and the freeze fallback can only ever
    raise ``file_journal_evidence_invariant_invalid``. The fourth guard (the
    former J6 param, ``accepted_submit_identity.py:620-623``) cannot be isolated
    at this entry point at all and now lives in
    ``test_reserve_rejects_a_streak_carried_without_a_decision``; J7/J8 are
    isolated by ``test_normalization_isolates_the_released_reservation_invariant``
    and ``test_normalization_isolates_the_foreign_decision_streak_invariant``.
    """

    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

    repository, public, durable = _accepted_submit_invariant_fixture(tmp_path, leg)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job({**public, **mutation})

    assert error.value.reason == expected_reason
    assert error.value.field == expected_field
    _assert_invariant_left_no_trace(repository, tmp_path, leg, public, durable)


def test_reserve_rejects_a_streak_carried_without_a_decision(tmp_path: Any) -> None:
    """#1180 J6: a fresh reservation cannot open with a non-zero streak.

    This guard (``accepted_submit_identity.py:620-623``) is unreachable in
    isolation through ``upsert_pipeline_job``: ``identity_blocked_streak`` sits
    in the #1183 ordinary-upsert freeze table
    (``ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS``), whose loop raises the
    identical ``(reason, field)`` before normalization can, so deleting the guard
    alone leaves that leg green.

    ``reserve_pipeline_job`` has no such fallback — its clean-reservation
    dirty-field set (``file_orchestration_journal.py:1779-1808``) deliberately
    omits the counter — so the insert path is where this guard alone decides.
    Zero-write is expressed as row absence rather than via
    ``_assert_invariant_left_no_trace``: on an insert there is no prior row, and
    that helper is unconditionally true at the upsert entry point anyway.
    """

    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalError,
        FileOrchestrationJournalRepository,
    )

    # Contamination control: the same shape with a zero streak reserves cleanly,
    # so a red run here is the guard talking and not a malformed record. Its own
    # repository, and the separation is load-bearing already at HEAD: the control
    # record carries the very job id the absence assertion below queries
    # (``_versioned_master_reservation_record(...)["job_id"] == _INVARIANT_JOB_ID``),
    # so a shared repository would make that assertion find the CONTROL's row and
    # go red with the guard intact. Under mutation it also matters: with the guard
    # deleted the illegal record lands and would turn this into an ordinary job-id
    # conflict. Do not consolidate the two repositories.
    control = FileOrchestrationJournalRepository(tmp_path / "streak-insert-control" / "journal")
    legal = _versioned_master_reservation_record(member_count=1)
    legal["identity_blocked_streak"] = 0
    assert control.reserve_pipeline_job(legal) is not None

    repository = FileOrchestrationJournalRepository(tmp_path / "streak-insert" / "journal")
    record = _versioned_master_reservation_record(member_count=1)
    record["identity_blocked_streak"] = 2
    record["reconciliation_decision"] = None

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.reserve_pipeline_job(record)

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "identity_blocked_streak"
    assert repository.get_pipeline_job(_INVARIANT_JOB_ID) is None


# ---------------------------------------------------------------------------
# #1180 J7/J8 isolation legs. Every durable write entry point measured so far
# (``upsert_pipeline_job`` / ``reserve_pipeline_job`` / the generic
# ``transition_pipeline_job_submit_evidence`` / the typed
# ``release_identity_blocked_reservation``) is blocked from isolating
# ``accepted_submit_identity.py:646-649`` and ``:650-653`` by something that
# raises first: the ordinary-upsert freeze table, the clean-reservation gate,
# the ``AcceptedSubmitTransition`` twin guards plus the decision whitelist, and a
# hard-coded ``status="reservation_lost"`` respectively. That enumeration is a
# measurement, not a proof that no entry point exists; if one is found these legs
# can be superseded. They are a SUPPLEMENT — the durable J7/J8 legs above and
# their zero-write assertions stay exactly as they were, which is what fixture
# review P1-5 was protecting. Measured across those same four entry points, no
# live caller can violate either site — i.e. both are purely defensive as far as
# anyone has measured, on the same basis and with the same limits as the
# enumeration above — so the direct call is the only oracle available today.
# ---------------------------------------------------------------------------


def _direct_normalization_payload(**mutation: Any) -> dict[str, Any]:
    """A contract-current master payload for direct normalization calls."""

    return {
        **_versioned_master_reservation_record(member_count=1),
        "status": "reserved",
        "submit_outcome": "submit_result_ambiguous",
        **mutation,
    }


def test_normalization_isolates_the_released_reservation_invariant() -> None:
    """#1180 J7 (isolation): ``identity_mismatch_released`` needs a lost reservation.

    Single-fault geometry on purpose — the streak stays 0 so the sibling guard at
    ``:650-653`` is structurally silent and cannot stand in for the one under
    test.
    """

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    legal = _direct_normalization_payload(
        reconciliation_decision="identity_mismatch_released",
        reconciliation_source="slurm_exact_comment",
        status="reservation_lost",
        identity_blocked_streak=0,
    )
    assert normalize_accepted_submit_evidence(legal)["status"] == "reservation_lost"

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            _direct_normalization_payload(
                reconciliation_decision="identity_mismatch_released",
                reconciliation_source="slurm_exact_comment",
                status="reserved",
                identity_blocked_streak=0,
            )
        )

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "reconciliation_decision"


def test_normalization_isolates_the_foreign_decision_streak_invariant() -> None:
    """#1180 J8 (isolation): the counter belongs to identity-mismatch decisions.

    Single-fault geometry: the only illegal thing about the payload is the streak
    riding an ``absence_retry_permitted`` decision.
    """

    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )

    legal = _direct_normalization_payload(
        reconciliation_decision="absence_retry_permitted",
        reconciliation_source="slurm_exact_comment",
        identity_blocked_streak=0,
    )
    assert normalize_accepted_submit_evidence(legal)["identity_blocked_streak"] == 0

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            _direct_normalization_payload(
                reconciliation_decision="absence_retry_permitted",
                reconciliation_source="slurm_exact_comment",
                identity_blocked_streak=1,
            )
        )

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "identity_blocked_streak"


# ---------------------------------------------------------------------------
# #1565: the name-window fallback source is legal only for matched_bound.
# ---------------------------------------------------------------------------


def test_name_window_unique_source_is_legal_only_for_matched_bound() -> None:
    """#1565 D3: ``slurm_name_window_unique`` may only ride ``matched_bound``;
    every other accounting decision stays ``slurm_exact_comment`` sourced."""
    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitTransition,
        apply_accepted_submit_transition,
        normalize_accepted_submit_evidence,
    )
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    valid = AcceptedSubmitTransition.accounting(
        "matched_bound",
        submit_outcome="accepted",
        matched_slurm_job_id="72001",
        status="submitted",
        reconciliation_source="slurm_name_window_unique",
    )
    assert valid.reconciliation_source == "slurm_name_window_unique"
    row = apply_accepted_submit_transition(
        {
            **_versioned_master_reservation_record(member_count=1),
            "status": "reserved",
            "submit_outcome": "submit_result_ambiguous",
        },
        valid,
    )
    assert row["reconciliation_source"] == "slurm_name_window_unique"
    assert row["reconciliation_decision"] == "matched_bound"
    assert normalize_accepted_submit_evidence(row)["reconciliation_source"] == "slurm_name_window_unique"

    for decision in (
        "accounting_unavailable",
        "identity_mismatch_blocked",
        "multiple_matches_blocked",
        "absence_deferred",
        "absence_retry_permitted",
    ):
        with pytest.raises(ValueError, match="name-window fallback source requires matched_bound"):
            AcceptedSubmitTransition.accounting(
                decision,
                submit_outcome="submit_result_ambiguous",
                status="reserved",
                reconciliation_source="slurm_name_window_unique",
            )


def test_name_window_unique_normalization_rejects_non_matched_bound() -> None:
    """#1565: the durable normalization boundary refuses the fallback source
    on any decision other than ``matched_bound``."""
    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitEvidenceError,
        normalize_accepted_submit_evidence,
    )
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    payload = {
        **_versioned_master_reservation_record(member_count=1),
        "status": "reserved",
        "submit_outcome": "submit_result_ambiguous",
        "reconciliation_source": "slurm_name_window_unique",
        "reconciliation_decision": "accounting_unavailable",
        "reconciliation_reason_class": "comment_accounting_unproven",
    }
    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(payload)
    assert error.value.field == "reconciliation_source"


def test_fallback_submit_unparsable_cannot_be_persisted_durably() -> None:
    """#1565 Fix 2: ``fallback_submit_unparsable`` is pass-evidence only. It is
    absent from the durable reason-class whitelist, so no accepted-submit
    transition (or normalization) can ever persist it."""
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_RECONCILIATION_REASON_CLASSES,
        AcceptedSubmitEvidenceError,
        AcceptedSubmitTransition,
        normalize_accepted_submit_evidence,
    )
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    assert "fallback_submit_unparsable" not in ACCEPTED_RECONCILIATION_REASON_CLASSES

    with pytest.raises(ValueError, match="invalid accepted-submit accounting reason class"):
        AcceptedSubmitTransition.accounting(
            "accounting_unavailable",
            submit_outcome="submit_result_ambiguous",
            status="reserved",
            reconciliation_reason_class="fallback_submit_unparsable",
        )

    with pytest.raises(AcceptedSubmitEvidenceError) as error:
        normalize_accepted_submit_evidence(
            {
                **_versioned_master_reservation_record(member_count=1),
                "status": "reserved",
                "submit_outcome": "submit_result_ambiguous",
                "reconciliation_source": "slurm_exact_comment",
                "reconciliation_decision": "accounting_unavailable",
                "reconciliation_reason_class": "fallback_submit_unparsable",
            }
        )
    assert error.value.field == "reconciliation_reason_class"
