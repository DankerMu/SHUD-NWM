"""#1565 bounded name-window fallback on a comment-less cluster: bind, deny and eligibility.

Partition of ``tests/test_gateway_reconcile_comment_accounting.py`` (#3177). The
fallback renders query bounds and parses Submit in the host local timezone, so
these command-shape tests pin the host TZ to UTC. The shared ``_fallback_row``
and ``_fallback_querier`` are in
``tests/gateway_reconcile_comment_accounting_helpers.py``.
"""

from __future__ import annotations

from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import Any

import pytest

from tests.gateway_reconcile_comment_accounting_helpers import _fallback_querier, _fallback_row
from tests.gateway_reconcile_helpers import _file_cohort_repository


def _pinned_utc_fallback_context(*, anchor: datetime, query_end: datetime):
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    return _pinned_local_timezone("UTC")


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_unique_binds_with_name_window_source_and_empty_comment_gate(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: an explicitly comment-less cluster + one owned in-window candidate
    with an empty accounting comment binds exactly once with
    ``slurm_name_window_unique`` / ``matched_bound``."""
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
        rows = _fallback_row("72001", submit="2026-07-12T01:00:00")
        query, commands = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.reconciliation_source == "slurm_name_window_unique"
        assert outcome.reconciliation_decision == "matched_bound"
        assert outcome.matched_slurm_job_id == "72001"
        assert outcome.match_count == 1
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["slurm_job_id"] == "72001"
        assert persisted["reconciliation_source"] == "slurm_name_window_unique"
        assert persisted["reconciliation_decision"] == "matched_bound"
        assert persisted["matched_slurm_job_id"] == "72001"
        assert len(commands) == 1
        command = commands[0]
        assert any(item == "--name=nhms_forecast" for item in command)
        # #2655: exactly one --format, SubmitLine appended last (the parser
        # reads it as the rejoined remainder after Submit).
        assert [item for item in command if item.startswith("--format=")] == [
            "--format=JobID,JobName,State,ExitCode,Comment,User,Account,Submit,SubmitLine"
        ]
        assert any(item == "--user=scheduler" for item in command)
        assert any(item == "--accounts=account" for item in command)
        assert any(item == "--starttime=2026-07-12T00:00:00" for item in command)
        assert any(item == f"--endtime={query_end.strftime('%Y-%m-%dT%H:%M:%S')}" for item in command)


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_comment_storing_cluster_exact_comment_lane_never_reads_submitline(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2655: on a comment-storing cluster the exact-comment lane keeps its own
    seven-field format, never issues the name-window query, and never consults
    the SubmitLine parser -- even when the reservation binds."""
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
        comment = "nhms_idem:cycle_gfs_2026071200_forecast_fixture:forecast"
        row = f"72001|nhms_forecast|RUNNING|0:0|{comment}|scheduler|account\n"
        commands: list[list[str]] = []
        monkeypatch.setattr(
            "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
            lambda command: commands.append(list(command)) or row,
        )

        def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("comment-storing lane must not parse SubmitLine")

        monkeypatch.setattr("services.orchestrator.reconcile_comment_sacct._parse_fallback_sacct_rows", _forbidden)
        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=reconcile_module.default_comment_sacct_querier(
                global_visibility_probe=lambda: True,
                comment_storage_probe=lambda: True,
                now=lambda: query_end,
            ),
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.reconciliation_source == "slurm_exact_comment"
        assert outcome.fallback_match_basis is None
        assert commands
        for command in commands:
            assert command[1:4] == [
                "--parsable2",
                "--noheader",
                "--format=JobID,JobName,State,ExitCode,Comment,User,Account",
            ]
            assert not any("SubmitLine" in item or item.startswith("--name=") for item in command)


def test_fallback_present_but_different_comment_stays_fatal_at_both_gates(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: a unique candidate whose comment is present-but-different is
    refused at the reserved identity gate; evidence is ``identity_mismatch_blocked``
    / count 1 and no bind/streak/identity-mismatch durable write occurs."""
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
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_decision"] == "accounting_unavailable"
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"
        assert persisted["reconciliation_source"] == "slurm_exact_comment"
        assert persisted["identity_blocked_streak"] == 0


def test_fallback_zero_candidates_is_not_an_absence_proof(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: zero eligible masters -> ``fallback_no_match`` / count 0; the row
    stays reserved and unbound with the durable held tuple, and no retry
    permission is written."""
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
        query, _ = _fallback_querier(monkeypatch, rows="", query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "fallback_no_match"
        assert outcome.match_count == 0
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_decision"] == "accounting_unavailable"
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_two_distinct_masters_is_ambiguous_and_never_binds(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: two distinct owned in-window masters -> ``ambiguous_fallback_match``
    with saturated count 2; the row stays held and disposal authority is
    unchanged."""
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
        rows = (
            _fallback_row("72001", submit="2026-07-12T01:00:00")
            + _fallback_row("72002", submit="2026-07-12T01:10:00")
            + _fallback_row("72003", submit="2026-07-12T01:20:00")
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "ambiguous_fallback_match"
        assert outcome.match_count == 2
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_decision"] == "accounting_unavailable"
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_malformed_submit_is_transient_denial_with_pass_only_reason(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: missing/unparsable Submit -> ``query_unavailable`` with pass-only
    ``fallback_submit_unparsable``; the durable held tuple is unchanged."""
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
        rows = _fallback_row("72001", submit="not-a-timestamp")
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "query_unavailable"
        assert outcome.reconciliation_reason_class == "fallback_submit_unparsable"
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_missing_submit_field_is_transient_denial(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: a row with all otherwise-eligible fields but a MISSING Submit
    field (seven fields, no trailing Submit) is malformed evidence -> transient
    denial ``fallback_submit_unparsable``; the durable held tuple is unchanged."""
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
        # Seven fields: JobID|JobName|State|ExitCode|Comment|User|Account — the
        # Submit field is absent entirely.
        rows = "72001|nhms_forecast|COMPLETED|0:0||scheduler|account\n"
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "query_unavailable"
        assert outcome.reconciliation_reason_class == "fallback_submit_unparsable"
        assert outcome.reconciliation_decision == "accounting_unavailable"
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"


def test_fallback_step_rows_normalize_and_deduplicate_to_bare_master(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565 Fix 5: master, array-member, and .batch/.extern step rows with a
    forecast-family job name all normalize to the same bare master id and
    deduplicate into ONE candidate, not ambiguity."""
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
        rows = "".join(
            [
                # Bare master row.
                "72001|nhms_forecast|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
                # Array member rows.
                "72001_0|nhms_forecast|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
                "72001_1|nhms_forecast|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
                # Step sub-rows (job name is the batch/extern step name).
                "72001.batch|batch|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
                "72001_0.batch|batch|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
                "72001.extern|extern|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n",
            ]
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "bound"
        assert outcome.matched_slurm_job_id == "72001"
        assert outcome.reconciliation_source == "slurm_name_window_unique"
        persisted = repository.get_accepted_submit_pipeline_job(pipeline_job_id)
        assert persisted["slurm_job_id"] == "72001"


def test_fallback_out_of_window_and_foreign_rows_are_ineligible(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: a submit instant outside the closed attempt window, a foreign
    user, and a foreign account never become candidates -> ``fallback_no_match``."""
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
        rows = (
            # In-window submit but foreign user/account.
            _fallback_row("72001", submit="2026-07-12T01:00:00", user="foreign", account="other")
            # Owned but out of the closed window (before the anchor).
            + _fallback_row("72002", submit="2026-07-11T23:00:00")
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "fallback_no_match"
        assert outcome.match_count == 0


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_fallback_forcing_job_name_is_ineligible_not_identity_mismatch(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6j: a non-forecast JobName (``nhms_forcing``) must be ineligible
    at the parser, so a query with ONLY such a row is ``fallback_no_match``
    with count 0 — never a wrong ``identity_mismatch_blocked``/1.

    The fallback is the FORECAST-family name window: forcing aliases,
    batch/extern step names, and unrelated names are not forecast candidates.
    With no other eligible rows the public outcome must be
    ``fallback_no_match``/0, the held tuple stays reserved/unbound with streak
    zero, and no bind occurs."""

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
        gfs_job_id = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
        rows = (
            # Owned, in-window, well-shaped master id — but a FORCING job name.
            _fallback_row("72001", submit="2026-07-12T01:00:00", job_name="nhms_forcing")
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.job_id == gfs_job_id
        assert outcome.action == "fallback_no_match"
        assert outcome.match_count == 0
        assert outcome.reconciliation_source == "slurm_exact_comment"
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
        persisted = repository.get_pipeline_job(gfs_job_id)
        assert persisted["status"] == "reserved"
        assert persisted["slurm_job_id"] is None
        assert persisted["identity_blocked_streak"] == 0


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")
def test_parse_fallback_sacct_rows_gates_forecast_family_job_name() -> None:
    """Phase 6j parser gate: ``_parse_fallback_sacct_rows`` accepts only the
    forecast family (``nhms_forecast`` and forecast stage aliases) and rejects
    ``nhms_forcing``, forcing aliases, batch/extern step names, and unrelated
    names — before owner/window classification, so a non-forecast row is
    ineligible (never ``fallback_submit_unparsable``)."""

    from datetime import UTC, datetime, timedelta

    from services.orchestrator import reconcile as reconcile_module
    from tests.test_real_slurm_gateway import _pinned_local_timezone

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    base_line = "72001|{job_name}|COMPLETED|0:0||scheduler|account|2026-07-12T01:00:00\n"
    with _pinned_local_timezone("UTC"):
        # Forecast family accepted (dedup to one bare master).
        records, parsable, _basis = reconcile_module._parse_fallback_sacct_rows(
            base_line.format(job_name="nhms_forecast")
            + base_line.format(job_name="forecast")
            + base_line.format(job_name="run_shud_forecast")
            + base_line.format(job_name="run_shud_forecast_array"),
            expected_user="scheduler",
            expected_account="account",
            window_start=anchor,
            window_end=query_end,
        )
        assert parsable is True
        assert [record.slurm_job_id for record in records] == ["72001"]

        # Forcing/unrelated/batch/extern job names rejected: zero candidates
        # and Submit stays parsable (ineligible rows never become transient
        # denial).
        for job_name in (
            "nhms_forcing",
            "forcing",
            "produce_forcing",
            "forcing_package",
            "batch",
            "extern",
            "unrelated",
        ):
            records, parsable, _basis = reconcile_module._parse_fallback_sacct_rows(
                base_line.format(job_name=job_name),
                expected_user="scheduler",
                expected_account="account",
                window_start=anchor,
                window_end=query_end,
            )
            assert records == [], f"job_name={job_name} must be ineligible"
            assert parsable is True, f"job_name={job_name} must not trigger submit_unparsable"


def test_fallback_deduplicates_array_members_to_bare_master_and_retains_at_most_two(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: array element rows normalize to the bare numeric master id and
    deduplicate; at most two distinct masters are retained."""
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
        rows = "".join(
            [
                _fallback_row("72001", submit="2026-07-12T01:00:00", task_id=0),
                _fallback_row("72001", submit="2026-07-12T01:00:00", task_id=1),
                _fallback_row("72001", submit="2026-07-12T01:00:00", task_id=2),
                _fallback_row("72002", submit="2026-07-12T01:10:00", task_id=0),
                _fallback_row("72002", submit="2026-07-12T01:10:00", task_id=1),
                _fallback_row("72003", submit="2026-07-12T01:20:00", task_id=0),
            ]
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=query_end)

        outcome = reconcile_module.reconcile_reserved_unbound_jobs(
            repository,
            comment_query=query,
            now=lambda: query_end,
        )[0]

        assert outcome.action == "ambiguous_fallback_match"
        assert outcome.match_count == 2


def test_fallback_does_not_run_when_ownership_is_incomplete(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: without expected user/account the fallback MAY NOT run; the row
    stays held under ``comment_accounting_unproven`` and no sacct query issues."""
    from services.orchestrator import reconcile as reconcile_module

    anchor = datetime(2026, 7, 12, tzinfo=UTC)
    query_end = anchor + timedelta(hours=2)
    repository = _file_cohort_repository(
        tmp_path,
        created_at=anchor,
        member_count=1,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: commands.append(list(command)) or "",
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
    assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
    assert commands == []


def test_fallback_unknown_capability_stays_query_free(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1565: a probe failure or missing AccountingStoreFlags line is unknown,
    never explicit no-comment; no fallback query issues."""
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
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: commands.append(list(command)) or "",
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: None,
        now=lambda: query_end,
    )

    outcome = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=query,
        now=lambda: query_end,
    )[0]

    assert outcome.action == "query_unavailable"
    assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
    assert commands == []
