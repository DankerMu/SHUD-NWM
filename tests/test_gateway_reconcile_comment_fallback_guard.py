"""#1565 name-window fallback: host-local Submit parse, bounded failures, the final bind guard
and #1850 Phase 6 runtime-mismatch lanes.

Partition of ``tests/test_gateway_reconcile_comment_accounting.py`` (#3177). The
host-local parse test pins Asia/Shanghai. The shared ``_fallback_row`` and
``_fallback_querier`` are in ``tests/gateway_reconcile_comment_accounting_helpers.py``.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from services.orchestrator.reconcile import SacctRecord
from tests.gateway_reconcile_comment_accounting_helpers import _fallback_querier, _fallback_row
from tests.gateway_reconcile_helpers import _file_cohort_repository


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_submit_is_parsed_in_host_local_timezone_to_utc(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: a timezone-less Slurm Submit value is interpreted in the host
    local timezone and converted to UTC before the window check (UTC+8 host)."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("Asia/Shanghai"):
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        # 2026-07-12T08:00:00+08 == 2026-07-12T00:00:00Z == the anchor: in-window.
        rows = _fallback_row("72001", submit="2026-07-12T08:00:00")
        query, commands = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        # The rendered query bounds are host-local wall-clock strings.
        assert any(item == "--starttime=2026-07-12T08:00:00" for item in commands[0])


@pytest.mark.parametrize(
    ("boundary", "reason_class", "payload", "max_rows", "max_bytes"),
    [
        ("bytes", "bounded_output_bytes_saturated", "123456789", 100, 8),
        ("rows", "bounded_output_rows_saturated", "\n\n\n", 2, 1024),
    ],
)
def test_fallback_bounded_failure_keeps_pass_reason_but_holds_tuple_as_unproven(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    reason_class: str,
    payload: str,
    max_rows: int,
    max_bytes: int,
) -> None:
    """#1565 D3: a fallback byte/row budget failure surfaces the applicable
    existing bounded-query reason as PASS evidence, but the durable held tuple
    stays ``slurm_exact_comment``/``accounting_unavailable``/
    ``comment_accounting_unproven`` (the #1564 CAS shape)."""
    from services.orchestrator import reconcile as reconcile_module

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    monkeypatch.setattr("services.orchestrator.reconcile_comment_sacct.MAX_COMMENT_SACCT_BYTES", max_bytes)
    monkeypatch.setattr("services.orchestrator.reconcile_comment_sacct.MAX_COMMENT_SACCT_ROWS", max_rows)
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda _command: payload,
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )

    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: query_end,
    )[0]

    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == reason_class
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_source"] == "slurm_exact_comment"
    assert persisted["reconciliation_decision"] == "accounting_unavailable"
    assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_process_failure_keeps_pass_reason_but_holds_tuple_as_unproven(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565 D3: a fallback subprocess failure surfaces ``process_unavailable``
    as PASS evidence, but the durable held tuple stays
    ``comment_accounting_unproven``."""
    from services.orchestrator import reconcile as reconcile_module

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"

    def failing_page(_command: Any) -> str:
        raise reconcile_module.ReconcileQueryUnavailable("sacct query returned 2")

    monkeypatch.setattr("services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout", failing_page)
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )

    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: query_end,
    )[0]

    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "process_unavailable"
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_source"] == "slurm_exact_comment"
    assert persisted["reconciliation_decision"] == "accounting_unavailable"
    assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_final_bind_guard_rejects_present_but_different_comment(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565 Fix 3: the FINAL bind guard is independent of the reserved
    identity gate. A unique candidate whose comment is present-but-different
    must be refused at the final gate even when the first gate is bypassed to
    accept everything: present-different can never reach commit."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00", comment="nhms_idem:other")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        commits: list[Any] = []
        original_committer = repository.commit_pipeline_job_submit_attempt

        def counting_committer(*args: Any, **kwargs: Any) -> Any:
            commits.append((args, kwargs))
            return original_committer(*args, **kwargs)

        monkeypatch.setattr(repository, "commit_pipeline_job_submit_attempt", counting_committer)
        # Bypass the reserved identity gate entirely (simulating a future
        # change/regression): the final bind guard must still refuse
        # present-but-different before commit is ever invoked.
        monkeypatch.setattr(
            "services.orchestrator.reconcile_reserved._reserved_record_identity_matches",
            lambda *_args, **_kwargs: True,
        )

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

    assert outcome.action == "identity_mismatch_blocked"
    assert outcome.match_count == 1
    assert commits == []
    persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert persisted["slurm_job_id"] is None


def test_fallback_final_bind_guard_allows_empty_and_same_comment_only(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565 Fix 3: the named final-gate predicate allows empty or the exact
    idempotency comment, and rejects any other present value."""
    from services.orchestrator.reconcile import _fallback_comment_allowed

    reservation_comment = "nhms_idem:cycle_gfs_2026071200_forecast_fixture:forecast"
    assert _fallback_comment_allowed(None, reservation_comment) is True
    assert _fallback_comment_allowed("", reservation_comment) is True
    assert _fallback_comment_allowed(reservation_comment, reservation_comment) is True
    assert _fallback_comment_allowed("nhms_idem:other", reservation_comment) is False


def test_fallback_held_row_remains_valid_for_guarded_operator_demotion(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565 D3: an unsuccessful fallback (zero candidates) preserves the
    durable #1564 held tuple byte-for-byte, so the guarded operator demotion
    CAS still accepts the row and converts it to ``operator_verified_absence``."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.orchestrator_demote_reserved_job_helpers import (
        _demote_kwargs,
        _held_row,
    )
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        query, _ = _fallback_querier(monkeypatch, rows="", query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

    assert outcome.action == "fallback_no_match"
    held = _held_row(repository)
    assert held["status"] == "reserved"
    assert held["slurm_job_id"] is None
    assert held["reconciliation_source"] == "slurm_exact_comment"
    assert held["reconciliation_decision"] == "accounting_unavailable"
    assert held["reconciliation_reason_class"] == "comment_accounting_unproven"

    receipt = repository.demote_operator_verified_reserved_job(
        pipeline_job_id,
        **_demote_kwargs(held),
    )
    assert receipt is not None
    demoted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
    assert demoted["status"] == "reservation_lost"
    assert demoted["reconciliation_decision"] == "operator_verified_absence"


# --- #1850 Phase 6: runtime identity disposition depends on the proven lane ---
# An exact-comment/unknown-capability runtime mismatch keeps the durable
# identity-blocked streak semantics; a unique candidate reached through the
# explicitly comment-less fallback that fails runtime identity stays in the
# held-tuple family with streak zero and never releases. The reserved gate is
# reached through a genuine runtime validator (no monkeypatched validator).


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_genuine_runtime_mismatch_stays_held_with_streak_zero(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1850 D2: an explicitly comment-less query yields one exclusive candidate
    but the reservation's genuine runtime identity is missing (no per-model
    ``hydro_run`` rows). The candidate does not bind, pass evidence is
    ``identity_mismatch_blocked``/1, the durable held tuple remains valid, and
    repeated passes keep ``identity_blocked_streak=0`` without release."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            with_runtime_rows=False,
        )
        pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        assert outcome.identity_blocked_streak is None
        persisted = repository.get_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_source"] == "slurm_exact_comment"
        assert persisted["reconciliation_decision"] == "accounting_unavailable"
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"
        assert persisted["identity_blocked_streak"] == 0

        # Repeated passes with the release ladder enabled still never release.
        for _ in range(3):
            repeat = reconcile_module.reconcile_reserved_unbound_jobs(
                repository,
                comment_query=query,
                accepted_submit_grace=timedelta(seconds=300),
                identity_blocked_streak_limit=2,
                now=lambda: query_end,
            )[0]
            assert repeat.action == "identity_mismatch_blocked"
            assert repeat.identity_blocked_streak is None
        held = repository.get_pipeline_job(pipeline_job_id)
        assert held["identity_blocked_streak"] == 0
        assert held["status"] == "reserved"
        assert held["reconciliation_source"] == "slurm_exact_comment"
        assert held["reconciliation_decision"] == "accounting_unavailable"
        assert held["reconciliation_reason_class"] == "comment_accounting_unproven"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_exact_comment_runtime_mismatch_retains_streak_and_release(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1850 D2: an exact-comment runtime mismatch keeps the durable
    identity-blocked streak semantics and the release ladder. With runtime
    identity missing on a comment-storing cluster, repeated passes increment
    the durable streak (exact-comment source) and eventually release."""
    from services.orchestrator import reconcile as reconcile_module

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        with_runtime_rows=False,
    )
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    exact = SacctRecord(
        "72001",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="scheduler",
        account="account",
    )

    def run_pass(limit: int | None = 3) -> Any:
        return reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=lambda _key: exact,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=limit,
            now=lambda: anchor + timedelta(hours=1),
        )[0]

    first = run_pass()
    assert first.action == "identity_mismatch_blocked"
    assert first.reconciliation_source == "slurm_exact_comment"
    assert first.reconciliation_decision == "identity_mismatch_blocked"
    assert first.identity_blocked_streak == 1
    assert repository.get_pipeline_job(pipeline_job_id)["identity_blocked_streak"] == 1

    second = run_pass()
    assert second.action == "identity_mismatch_blocked"
    assert second.identity_blocked_streak == 2
    assert repository.get_pipeline_job(pipeline_job_id)["status"] == "reserved"

    released = run_pass()
    assert released.action == "identity_mismatch_released"
    assert released.status == "reservation_lost"
    assert released.reconciliation_decision == "identity_mismatch_released"
    assert released.identity_blocked_streak == 3
    assert repository.get_pipeline_job(pipeline_job_id)["status"] == "reservation_lost"


# --- #1850 Phase 6c: pre-outcome rows must use the same capability/runtime lane
# as already-timeout rows (Fix 1). An identical runtime-invalid row with
# submit_outcome=None must land in the same held/streak-zero family on an
# explicitly comment-less cluster, and the same ordinary streak family when
# capability is True/unknown.


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_pre_outcome_runtime_mismatch_matches_timeout_lane(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1: a pre-outcome (submit_outcome=None) runtime-invalid forecast row
    on an explicitly comment-less cluster behaves identically to an
    already-timeout row: held tuple, streak zero, never released."""
    from services.orchestrator import reconcile as reconcile_module
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    with _pinned_local_timezone("UTC"):
        # Pre-outcome row: no timeout transition written yet.
        repository = _file_cohort_repository(
            tmp_path,
            created_at=anchor,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
            with_runtime_rows=False,
            submit_outcome=None,
        )
        pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "identity_mismatch_blocked"
        assert outcome.match_count == 1
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_source"] == "slurm_exact_comment"
        assert persisted["reconciliation_decision"] == "accounting_unavailable"
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"
        assert persisted["identity_blocked_streak"] == 0

        # Repeated passes with the release ladder enabled still never release.
        for _ in range(3):
            repeat = reconcile_module.reconcile_reserved_unbound_jobs(
                repository,
                comment_query=query,
                accepted_submit_grace=timedelta(seconds=300),
                identity_blocked_streak_limit=2,
                now=lambda: query_end,
            )[0]
            assert repeat.action == "identity_mismatch_blocked"
            assert repeat.identity_blocked_streak is None
        held = repository.get_pipeline_job(pipeline_job_id)
        assert held["identity_blocked_streak"] == 0
        assert held["status"] == "reserved"


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_pre_outcome_exact_comment_runtime_mismatch_matches_ordinary_streak(
    tmp_path: Any,
) -> None:
    """Fix 1: a pre-outcome runtime-invalid forecast row on a comment-storing /
    unknown-capability cluster keeps the ordinary exact-comment streak and
    release ladder, exactly like an already-timeout row."""
    from services.orchestrator import reconcile as reconcile_module

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        with_runtime_rows=False,
        submit_outcome=None,
    )
    pipeline_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    exact = SacctRecord(
        "72001",
        "COMPLETED",
        "nhms_forecast",
        comment=f"nhms_idem:{key}",
        user="scheduler",
        account="account",
    )

    def run_pass(limit: int | None = 3) -> Any:
        return reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=lambda _key: exact,
            accepted_submit_grace=timedelta(seconds=300),
            identity_blocked_streak_limit=limit,
            now=lambda: anchor + timedelta(hours=1),
        )[0]

    first = run_pass()
    assert first.action == "identity_mismatch_blocked"
    assert first.reconciliation_source == "slurm_exact_comment"
    assert first.reconciliation_decision == "identity_mismatch_blocked"
    assert first.identity_blocked_streak == 1
    assert repository.get_pipeline_job(pipeline_job_id)["identity_blocked_streak"] == 1

    second = run_pass()
    assert second.action == "identity_mismatch_blocked"
    assert second.identity_blocked_streak == 2

    released = run_pass()
    assert released.action == "identity_mismatch_released"
    assert released.status == "reservation_lost"
    assert released.reconciliation_decision == "identity_mismatch_released"
    assert released.identity_blocked_streak == 3
    assert repository.get_pipeline_job(pipeline_job_id)["status"] == "reservation_lost"
