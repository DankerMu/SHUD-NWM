"""#2655: comment-less fallback binds by the exact idempotency key in SubmitLine.

node-22 is explicitly comment-less (``AccountingStoreFlags=(null)``), so the
#1565 name-window fallback can only count ``nhms_forecast`` masters. When the
gfs and IFS forecast cohorts are submitted seconds apart each reserved-unbound
master sees both arrays and stays ``ambiguous_fallback_match`` forever, even
though ``sacct SubmitLine`` carries ``--comment=nhms_idem:<key>`` on every
array-task row. These tests drive the real restart-reconcile seam (fake sacct
stdout through ``_bounded_sacct_stdout``) against a real
``FileOrchestrationJournalRepository``.

Fixture rows mirror the production shape measured on node-22 (2026-09-27):
every array-task allocation row (``56823_0``) carries
``/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:<key> /tmp/nhms_<x>.sbatch``,
and ``.batch`` / ``.extern`` step rows carry an empty SubmitLine and no user.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
)
from tests.gateway_reconcile_helpers import (
    _append_cohort_placeholders,
    _file_cohort_repository,
)
from tests.test_gateway_reconcile_claimant_exclusivity import (
    _fallback_querier,
    _fallback_row,
    _second_master_reservation_record,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only"
)

GFS_KEY = "cycle_gfs_2026071200_forecast_fixture:forecast"
IFS_KEY = "cycle_ifs_2026071200_forecast_fixture:forecast"
GFS_JOB = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
IFS_JOB = "job_cycle_ifs_2026071200_forecast_fixture_forecast"
# The IFS cohort is reserved ~13 s before gfs in the same scheduler pass
# (production: 16:42:42Z vs 16:42:55Z); both arrays are accepted ~2 min later.
IFS_ANCHOR = datetime(2026, 7, 12, 0, 0, 0, tzinfo=UTC)
GFS_ANCHOR = datetime(2026, 7, 12, 0, 0, 13, tzinfo=UTC)
IFS_ARRAY_SUBMIT = "2026-07-12T00:02:17"
GFS_ARRAY_SUBMIT = "2026-07-12T00:02:23"
QUERY_END = datetime(2026, 7, 12, 2, 0, 0, tzinfo=UTC)


def _submit_line(key: str, *, script: str = "/tmp/nhms_6y2bx17b.sbatch") -> str:
    return f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{key} {script}"


def _array_rows(
    master_id: str,
    *,
    submit: str,
    submit_line: str | None,
    tasks: int = 3,
) -> str:
    """Production-shaped rows for one array: task rows + empty batch/extern steps."""

    rows = []
    for task in range(tasks):
        rows.append(_fallback_row(f"{master_id}_{task}", submit=submit, submit_line=submit_line))
        for step in ("batch", "extern"):
            rows.append(f"{master_id}_{task}.{step}|{step}|COMPLETED|0:0|||account|{submit}|\n")
    return "".join(rows)


def _reserve_ifs_sibling(repository: Any, *, member_status: str | None = None, members: int = 1) -> None:
    record = _second_master_reservation_record(
        source_id="ifs",
        created_at=IFS_ANCHOR,
        member_count=members,
        expected_user="scheduler",
        expected_account="account",
    )
    repository.reserve_pipeline_job(record)
    repository.transition_pipeline_job_submit_evidence(
        record["job_id"],
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    _append_cohort_placeholders(
        repository,
        members,
        source_id="ifs",
        common_updates=(
            {"status": member_status, "error_code": None, "error_message": None}
            if member_status
            else None
        ),
    )


def _concurrent_pair(tmp_path: Path, *, member_status: str | None = None, members: int = 1) -> Any:
    repository = _file_cohort_repository(
        tmp_path / "pair",
        created_at=GFS_ANCHOR,
        member_count=members,
        expected_user="scheduler",
        expected_account="account",
        with_runtime_rows=member_status is None,
    )
    if member_status is not None:
        _append_cohort_placeholders(
            repository,
            members,
            source_id="gfs",
            common_updates={"status": member_status, "error_code": None, "error_message": None},
        )
    _reserve_ifs_sibling(repository, member_status=member_status, members=members)
    return repository


def _journal_bytes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_held(repository: Any, job_id: str) -> None:
    persisted = repository.get_pipeline_job(job_id)
    assert persisted["status"] == "reserved"
    assert persisted["slurm_job_id"] is None
    assert persisted["reconciliation_source"] == "slurm_exact_comment"
    assert persisted["reconciliation_decision"] == "accounting_unavailable"
    assert persisted["reconciliation_reason_class"] == "comment_accounting_unproven"
    assert persisted["identity_blocked_streak"] == 0
    assert "fallback_match_basis" not in persisted


# ---------------------------------------------------------------------------
# Parser: literal production rows
# ---------------------------------------------------------------------------


def test_parser_resolves_production_0925_rows_by_submitline_key() -> None:
    """The literal 0925 incident rows: each reservation comment selects its own
    array, the other source's array is excluded, and ``.batch`` rows with an
    empty SubmitLine (ineligible by name/user) never make a master unknown."""

    from services.orchestrator import reconcile as reconcile_module

    ifs_key = "cycle_ifs_2026092500_convert_cohort_34e13d82a8a5:forecast"
    gfs_key = "cycle_gfs_2026092500_convert_cohort_caaad82942af:forecast"
    anchor = datetime(2026, 9, 25, 16, 42, 42, tzinfo=UTC)
    end = datetime(2026, 9, 26, 14, 24, 58, tzinfo=UTC)
    stdout = "".join(
        [
            "56823_0|nhms_forecast|COMPLETED|0:0||nwm|nwm|2026-09-25T16:44:59|"
            f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{ifs_key} /tmp/nhms_6y2bx17b.sbatch\n",
            "56823_0.batch|batch|COMPLETED|0:0|||nwm|2026-09-25T16:44:59|\n",
            "56823_0.extern|extern|COMPLETED|0:0|||nwm|2026-09-25T16:44:59|\n",
            "56839_0|nhms_forecast|COMPLETED|0:0||nwm|nwm|2026-09-25T16:45:05|"
            f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{gfs_key} /tmp/nhms_q1w2e3r4.sbatch\n",
            "56839_0.batch|batch|COMPLETED|0:0|||nwm|2026-09-25T16:45:05|\n",
            "56823_1|nhms_forecast|COMPLETED|0:0||nwm|nwm|2026-09-25T16:44:59|"
            f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{ifs_key} /tmp/nhms_6y2bx17b.sbatch\n",
            "56839_1|nhms_forecast|COMPLETED|0:0||nwm|nwm|2026-09-25T16:45:05|"
            f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{gfs_key} /tmp/nhms_q1w2e3r4.sbatch\n",
        ]
    )
    with _pinned_local_timezone("UTC"):
        for key, expected in ((ifs_key, "56823"), (gfs_key, "56839")):
            records, parsable, basis = reconcile_module._parse_fallback_sacct_rows(
                stdout,
                expected_user="nwm",
                expected_account="nwm",
                window_start=anchor,
                window_end=end,
                reservation_comment=f"nhms_idem:{key}",
            )
            assert parsable is True
            assert basis == "submitline_exact"
            assert [record.slurm_job_id for record in records] == [expected]
            assert records[0].submitline_key == f"nhms_idem:{key}"

        # A SubmitLine may itself contain ``|``: it is the rejoined remainder.
        piped = (
            "56823_0|nhms_forecast|COMPLETED|0:0||nwm|nwm|2026-09-25T16:44:59|"
            f"/usr/bin/sbatch --array=0-46%15 --comment=nhms_idem:{ifs_key} --wrap=a|b\n"
        )
        records, _parsable, basis = reconcile_module._parse_fallback_sacct_rows(
            piped,
            expected_user="nwm",
            expected_account="nwm",
            window_start=anchor,
            window_end=end,
            reservation_comment=f"nhms_idem:{ifs_key}",
        )
        assert basis == "submitline_exact"
        assert [record.slurm_job_id for record in records] == ["56823"]


def test_parser_decides_the_basis_over_every_master_not_the_first_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two equal-key masters followed by an unknown-key master: the basis is
    decided over ALL eligible masters, so the late unknown key forces
    ``name_window_count`` (no early stop may label the window
    ``submitline_exact``). Classification stays ambiguous and held."""

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    rows = (
        _array_rows("56839", submit=GFS_ARRAY_SUBMIT, submit_line=_submit_line(GFS_KEY))
        + _array_rows("56840", submit="2026-07-12T00:02:40", submit_line=_submit_line(GFS_KEY))
        + _array_rows("56841", submit="2026-07-12T00:02:50", submit_line=None)
    )
    with _pinned_local_timezone("UTC"):
        records, parsable, basis = reconcile_module._parse_fallback_sacct_rows(
            rows,
            expected_user="scheduler",
            expected_account="account",
            window_start=GFS_ANCHOR,
            window_end=QUERY_END,
            reservation_comment=f"nhms_idem:{GFS_KEY}",
        )
        assert parsable is True
        assert basis == "name_window_count"
        assert [record.slurm_job_id for record in records] == ["56839", "56840"]

        repository = _file_cohort_repository(
            tmp_path / "late_unknown",
            created_at=GFS_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (outcome,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)

        assert outcome.action == "ambiguous_fallback_match"
        assert outcome.match_count == 2
        assert outcome.fallback_match_basis == "name_window_count"
        _assert_held(repository, GFS_JOB)


# ---------------------------------------------------------------------------
# Restart reconcile: submitline_exact binds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["gfs_then_ifs", "ifs_then_gfs", "single_pass"])
def test_concurrent_sibling_sources_each_bind_their_own_array(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """The #2655 incident shape: both windows contain both arrays; each
    reservation excludes the other source's array and binds its own exactly
    once, independent of reconcile iteration order."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs
    from services.orchestrator.scheduler_runtime import _serialize_reserved_unbound_outcome

    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path)
        rows = _array_rows("56823", submit=IFS_ARRAY_SUBMIT, submit_line=_submit_line(IFS_KEY)) + _array_rows(
            "56839", submit=GFS_ARRAY_SUBMIT, submit_line=_submit_line(GFS_KEY)
        )
        outcomes: dict[str, Any] = {}
        if order == "single_pass":
            query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
            for outcome in reconcile_reserved_unbound_jobs(
                repository, comment_query=query, now=lambda: QUERY_END
            ):
                outcomes[outcome.job_id] = outcome
        else:
            sequence = [GFS_JOB, IFS_JOB] if order == "gfs_then_ifs" else [IFS_JOB, GFS_JOB]
            for job_id in sequence:
                query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
                (outcome,) = reconcile_reserved_unbound_jobs(
                    repository,
                    comment_query=query,
                    now=lambda: QUERY_END,
                    target_job_id=job_id,
                )
                outcomes[job_id] = outcome

        expected = {
            GFS_JOB: ("56839", "2026-07-12T00:02:23Z"),
            IFS_JOB: ("56823", "2026-07-12T00:02:17Z"),
        }
        assert set(outcomes) == set(expected)
        for job_id, (master, accounting_submit) in expected.items():
            outcome = outcomes[job_id]
            assert outcome.action == "bound", (job_id, outcome)
            assert outcome.slurm_job_id == master
            assert outcome.reconciliation_source == "slurm_name_window_unique"
            assert outcome.reconciliation_decision == "matched_bound"
            assert outcome.matched_slurm_job_id == master
            assert outcome.match_count == 1
            assert outcome.fallback_match_basis == "submitline_exact"
            persisted = repository.get_pipeline_job(job_id)
            assert persisted["status"] == "submitted"
            assert persisted["slurm_job_id"] == master
            assert persisted["reconciliation_source"] == "slurm_name_window_unique"
            assert persisted["reconciliation_decision"] == "matched_bound"
            # No new durable token: the existing fallback provenance tuple.
            assert persisted["slurm_binding_source"] == "slurm_name_window_unique"
            assert persisted["slurm_accounting_submitted_at"] == accounting_submit
            assert "fallback_match_basis" not in persisted
            serialized = _serialize_reserved_unbound_outcome(repository, outcome)
            assert serialized["fallback_match_basis"] == "submitline_exact"
            assert serialized["slurm_binding_source"] == "slurm_name_window_unique"


def test_wide_window_with_107_foreign_masters_binds_the_exact_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 0911 anchor window held 107 owned forecast masters: every foreign
    key is excluded before the two-master cap and the equal master binds."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "wide",
            created_at=GFS_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = []
        for index in range(107):
            master = str(47000 + index)
            foreign = f"cycle_gfs_20260712{index:02d}_other_{index}:forecast"
            rows.append(
                _array_rows(master, submit="2026-07-12T00:30:00", submit_line=_submit_line(foreign), tasks=2)
            )
            if index == 60:
                rows.append(
                    _array_rows("47826", submit="2026-07-12T00:31:00", submit_line=_submit_line(GFS_KEY), tasks=2)
                )
        query, _ = _fallback_querier(monkeypatch, rows="".join(rows), query_end=QUERY_END)

        (outcome,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)

        assert outcome.action == "bound"
        assert outcome.slurm_job_id == "47826"
        assert outcome.fallback_match_basis == "submitline_exact"
        persisted = repository.get_pipeline_job(GFS_JOB)
        assert persisted["slurm_job_id"] == "47826"
        assert persisted["slurm_binding_source"] == "slurm_name_window_unique"


def test_window_with_only_foreign_keys_is_no_match_and_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every eligible master provably belongs to another reservation: zero
    remaining masters -> ``fallback_no_match`` (never an absence proof), held
    tuple, and no match basis because no remaining master was classified."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs
    from services.orchestrator.scheduler_runtime import _serialize_reserved_unbound_outcome

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "foreign",
            created_at=GFS_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _array_rows("56823", submit=IFS_ARRAY_SUBMIT, submit_line=_submit_line(IFS_KEY))
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)

        (outcome,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)

        assert outcome.action == "fallback_no_match"
        assert outcome.match_count == 0
        assert outcome.fallback_match_basis is None
        assert "fallback_match_basis" not in _serialize_reserved_unbound_outcome(repository, outcome)
        _assert_held(repository, GFS_JOB)


# ---------------------------------------------------------------------------
# Fail-closed cases
# ---------------------------------------------------------------------------


def test_pipeline_jobs_residue_quarantines_a_classified_bind_and_keeps_the_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The node-22 rehearsal shape: a non-``.json`` operator residue under
    ``pipeline-jobs/`` makes the commit-time claimant scan raise AFTER the
    window classified ``submitline_exact``. The row is ``journal_quarantined``
    (fail-closed, held tuple unchanged) and pass evidence still names the
    basis that was classified."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs
    from services.orchestrator.scheduler_runtime import _serialize_reserved_unbound_outcome

    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path)
        rows = _array_rows("56823", submit=IFS_ARRAY_SUBMIT, submit_line=_submit_line(IFS_KEY)) + _array_rows(
            "56839", submit=GFS_ARRAY_SUBMIT, submit_line=_submit_line(GFS_KEY)
        )
        # Establish the durable held tuple first (the production rows already
        # carry it), then drop the residue into the journal root.
        double = _array_rows("56840", submit="2026-07-12T00:02:40", submit_line=_submit_line(GFS_KEY)) + _array_rows(
            "56841", submit="2026-07-12T00:02:41", submit_line=_submit_line(GFS_KEY)
        )
        query, _ = _fallback_querier(monkeypatch, rows=double, query_end=QUERY_END)
        (held,) = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: QUERY_END, target_job_id=GFS_JOB
        )
        assert held.action == "ambiguous_fallback_match"
        _assert_held(repository, GFS_JOB)
        residue = (
            repository.root
            / "pipeline-jobs"
            / "job_cycle_gfs_2026072300_convert_cohort_29a594caa8bc_forecast.json.bak-zombie-20260808"
        )
        residue.write_text("{}", encoding="utf-8")
        before = _journal_bytes(repository.root)

        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (outcome,) = reconcile_reserved_unbound_jobs(
            repository, comment_query=query, now=lambda: QUERY_END, target_job_id=GFS_JOB
        )

        assert outcome.action == "journal_quarantined"
        assert outcome.quarantine_reason == "file_journal_reconcile_inventory_migration_invalid"
        assert outcome.quarantine_field == "pipeline_jobs"
        assert outcome.fallback_match_basis == "submitline_exact"
        serialized = _serialize_reserved_unbound_outcome(repository, outcome)
        assert serialized["action"] == "journal_quarantined"
        assert serialized["fallback_match_basis"] == "submitline_exact"
        assert _journal_bytes(repository.root) == before
        _assert_held(repository, GFS_JOB)



def test_two_masters_with_the_same_key_stay_ambiguous_with_byte_identical_held_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine double submission (two masters carrying this reservation's
    key) is never collapsed into a bind."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "double",
            created_at=GFS_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _array_rows("56839", submit=GFS_ARRAY_SUBMIT, submit_line=_submit_line(GFS_KEY)) + _array_rows(
            "56840", submit="2026-07-12T00:02:40", submit_line=_submit_line(GFS_KEY, script="/tmp/nhms_zz.sbatch")
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (first,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)
        assert first.action == "ambiguous_fallback_match"
        _assert_held(repository, GFS_JOB)
        held_bytes = _journal_bytes(repository.root)

        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (second,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)

        for outcome in (first, second):
            assert outcome.action == "ambiguous_fallback_match"
            assert outcome.match_count == 2
            assert outcome.reconciliation_decision == "accounting_unavailable"
            assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
            assert outcome.fallback_match_basis == "submitline_exact"
        assert second.durable_write_count == 0
        assert _journal_bytes(repository.root) == held_bytes
        _assert_held(repository, GFS_JOB)


def test_known_foreign_master_plus_unknown_key_master_stays_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unknown key disables exclusion for the whole window: a provably
    foreign master plus a SubmitLine-less master is still two masters."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _file_cohort_repository(
            tmp_path / "mixed",
            created_at=GFS_ANCHOR,
            member_count=1,
            expected_user="scheduler",
            expected_account="account",
        )
        rows = _array_rows("56823", submit=IFS_ARRAY_SUBMIT, submit_line=_submit_line(IFS_KEY)) + _array_rows(
            "56839", submit=GFS_ARRAY_SUBMIT, submit_line=None
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (first,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)
        held_bytes = _journal_bytes(repository.root)
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        (second,) = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)

        for outcome in (first, second):
            assert outcome.action == "ambiguous_fallback_match"
            assert outcome.match_count == 2
            assert outcome.fallback_match_basis == "name_window_count"
        assert second.durable_write_count == 0
        assert _journal_bytes(repository.root) == held_bytes
        _assert_held(repository, GFS_JOB)


_UNKNOWN_KEY_VARIANTS = {
    # Older sacct / no SubmitLine column: eight fields.
    "eight_field": [None],
    "empty": [""],
    "key_less": ["/usr/bin/sbatch --array=0-46%15 /tmp/nhms_6y2bx17b.sbatch"],
    "multi_valued": [f"/usr/bin/sbatch --comment=nhms_idem:{GFS_KEY} --comment=nhms_idem:{IFS_KEY} /tmp/x.sbatch"],
    # Inconsistent master: a foreign-key row first, then an equal-key row.
    "inconsistent_foreign_then_equal": [
        _submit_line("cycle_gfs_2026071100_other:forecast"),
        _submit_line(GFS_KEY),
    ],
    # Inconsistent master: a foreign-key row first, then a key-less row.
    "inconsistent_foreign_then_keyless": [
        _submit_line("cycle_gfs_2026071100_other:forecast"),
        "/usr/bin/sbatch --array=0-46%15 /tmp/nhms_6y2bx17b.sbatch",
    ],
}


@pytest.mark.parametrize("variant", sorted(_UNKNOWN_KEY_VARIANTS))
def test_unknown_key_keeps_pre_change_classification_and_claimant_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """Any unknown master key keeps count-only semantics: one eligible master
    in two overlapping reservation windows still has two durable claimants,
    so neither binds (the pre-#2655 outcome), and pass evidence only gains
    ``fallback_match_basis=name_window_count``."""

    from services.orchestrator.reconcile import reconcile_reserved_unbound_jobs

    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path)
        rows = "".join(
            _fallback_row(f"72001_{task}", submit="2026-07-12T00:05:00", submit_line=line)
            for task, line in enumerate(_UNKNOWN_KEY_VARIANTS[variant])
        )
        query, _ = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        outcomes = {
            outcome.job_id: outcome
            for outcome in reconcile_reserved_unbound_jobs(
                repository, comment_query=query, now=lambda: QUERY_END
            )
        }

        assert set(outcomes) == {GFS_JOB, IFS_JOB}
        for job_id in (GFS_JOB, IFS_JOB):
            outcome = outcomes[job_id]
            assert outcome.action == "ambiguous_fallback_match", (variant, job_id, outcome)
            assert outcome.match_count == 2
            assert outcome.reconciliation_decision == "accounting_unavailable"
            assert outcome.reconciliation_reason_class == "comment_accounting_unproven"
            assert outcome.fallback_match_basis == "name_window_count"
            _assert_held(repository, job_id)


# ---------------------------------------------------------------------------
# Typed commit: key re-verification and keyed claimant rule
# ---------------------------------------------------------------------------


def _name_window_transition(slurm_job_id: str) -> AcceptedSubmitTransition:
    return AcceptedSubmitTransition.accounting(
        "matched_bound",
        submit_outcome="accepted",
        matched_slurm_job_id=slurm_job_id,
        status="submitted",
        reconciliation_source="slurm_name_window_unique",
    )


def test_commit_refuses_a_submitline_key_that_is_not_the_reservations_own(tmp_path: Path) -> None:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path)
        before = _journal_bytes(repository.root)

        refused = repository.commit_pipeline_job_submit_attempt(
            GFS_KEY,
            pipeline_job_id=GFS_JOB,
            expected_submission_attempt=1,
            slurm_job_id="56823",
            slurm_accounting_submitted_at=datetime(2026, 7, 12, 0, 2, 17, tzinfo=UTC),
            transition=_name_window_transition("56823"),
            fallback_submitline_key=f"nhms_idem:{IFS_KEY}",
        )

        assert refused.committed is False
        assert refused.outcome == "identity_mismatch_blocked"
        assert _journal_bytes(repository.root) == before
        reopened = FileOrchestrationJournalRepository(repository.root)
        assert reopened.get_pipeline_job(GFS_JOB)["slurm_job_id"] is None
        assert reopened.get_pipeline_job(GFS_JOB)["status"] == "reserved"

        # A key on a non-name-window bind is a caller-contract violation.
        with pytest.raises(ValueError):
            repository.commit_pipeline_job_submit_attempt(
                GFS_KEY,
                pipeline_job_id=GFS_JOB,
                expected_submission_attempt=1,
                slurm_job_id="56839",
                transition=AcceptedSubmitTransition.accepted(status="submitted"),
                fallback_submitline_key=f"nhms_idem:{GFS_KEY}",
            )
        assert _journal_bytes(repository.root) == before


def test_commit_claimant_scan_counts_only_same_key_siblings_under_a_proven_key(tmp_path: Path) -> None:
    """Without a key the overlapping IFS sibling is a claimant (pre-change
    #1850 rule); with this reservation's own proven key it is not."""

    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path)
        submit = datetime(2026, 7, 12, 0, 2, 23, tzinfo=UTC)

        unkeyed = repository.commit_pipeline_job_submit_attempt(
            GFS_KEY,
            pipeline_job_id=GFS_JOB,
            expected_submission_attempt=1,
            slurm_job_id="56839",
            slurm_accounting_submitted_at=submit,
            transition=_name_window_transition("56839"),
        )
        assert unkeyed.outcome == "ambiguous_fallback_match"
        assert repository.get_pipeline_job(GFS_JOB)["slurm_job_id"] is None

        keyed = repository.commit_pipeline_job_submit_attempt(
            GFS_KEY,
            pipeline_job_id=GFS_JOB,
            expected_submission_attempt=1,
            slurm_job_id="56839",
            slurm_accounting_submitted_at=submit,
            transition=_name_window_transition("56839"),
            fallback_submitline_key=f"nhms_idem:{GFS_KEY}",
        )
        assert keyed.outcome == "applied"
        persisted = repository.get_pipeline_job(GFS_JOB)
        assert persisted["slurm_job_id"] == "56839"
        assert persisted["slurm_binding_source"] == "slurm_name_window_unique"
        # The keyed bind never touched the sibling reservation.
        assert repository.get_pipeline_job(IFS_JOB)["status"] == "reserved"
        assert repository.get_pipeline_job(IFS_JOB)["slurm_job_id"] is None

        # Active-owner occupancy is unchanged under a proven key: the IFS row
        # cannot bind 56839 even when the caller claims IFS's own key.
        occupied = repository.commit_pipeline_job_submit_attempt(
            IFS_KEY,
            pipeline_job_id=IFS_JOB,
            expected_submission_attempt=1,
            slurm_job_id="56839",
            slurm_accounting_submitted_at=submit,
            transition=_name_window_transition("56839"),
            fallback_submitline_key=f"nhms_idem:{IFS_KEY}",
        )
        assert occupied.outcome == "active_slurm_id_occupied"
        assert repository.get_pipeline_job(IFS_JOB)["slurm_job_id"] is None


# ---------------------------------------------------------------------------
# AC5: bound COMPLETED cohort projects terminal and frees the scheduler
# ---------------------------------------------------------------------------


def _scheduler_candidate(source: str, index: int) -> Any:
    from services.orchestrator import scheduler as scheduler_module
    from services.orchestrator.chain_config import scenario_for_source

    canonical = "gfs" if source == "gfs" else "IFS"
    scenario = scenario_for_source(canonical)
    return scheduler_module.SchedulerCandidate(
        candidate_id=f"{canonical}:2026-07-12T00:00:00Z:model_{index}:{scenario}",
        source_id=canonical,
        cycle_id=f"{source}_2026071200",
        cycle_time_utc=datetime(2026, 7, 12, tzinfo=UTC),
        model_id=f"model_{index}",
        basin_id=f"basin_{index}",
        basin_version_id=f"basin_v{index}",
        river_network_version_id=f"river_v{index}",
        segment_count=1,
        output_segment_count=1,
        model_package_uri=f"s3://nhms/models/model_{index}.tar",
        resource_profile={},
        display_capabilities={},
        horizon={},
        scenario_id=scenario,
        run_id=f"fcst_{source}_2026071200_model_{index}",
        forcing_version_id=f"forc_{source}_2026071200_model_{index}",
        status="ready",
    )


def _state_decision(repository: Any, source: str, index: int) -> Any:
    from services.orchestrator import scheduler as scheduler_module

    candidate = _scheduler_candidate(source, index)
    state = repository.candidate_state(
        source_id=candidate.source_id,
        cycle_time=candidate.cycle_time_utc,
        model_id=candidate.model_id,
        run_id=candidate.run_id,
        forcing_version_id=candidate.forcing_version_id,
        candidate_id=candidate.candidate_id,
    )
    return scheduler_module._candidate_state_decision(candidate, state)


def test_bound_completed_cohort_projects_terminal_and_is_no_longer_active_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0925 shape end to end: members at ``hydro_run=created`` behind a held
    reserved master are ``active_duplicate_pipeline``; after the SubmitLine
    bind and one inflight pass over COMPLETED accounting the members are no
    longer active and the scheduler stops skipping them. The finished SHUD
    forecast is not resubmitted: each member resumes after the completed stage
    (``restart_stage=state_save_qc``, ``native_shud_resubmitted=False``)."""

    from services.orchestrator.reconcile import (
        SacctRecord,
        reconcile_inflight_jobs,
        reconcile_reserved_unbound_jobs,
    )

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    members = 2
    with _pinned_local_timezone("UTC"):
        repository = _concurrent_pair(tmp_path, member_status="created", members=members)
        # Production shape: the ambiguous forecast submit leaves the held
        # reserved master code-less (the cause lives on the event only).
        for job_id in (GFS_JOB, IFS_JOB):
            held = repository.get_pipeline_job(job_id)
            assert held["status"] == "reserved"
            assert held["submit_outcome"] == "submit_result_ambiguous"
            assert held["error_code"] is None

        cycle_time = datetime(2026, 7, 12, tzinfo=UTC)
        for source, canonical in (("gfs", "gfs"), ("ifs", "IFS")):
            for index in range(members):
                assert repository.has_active_pipeline(
                    source_id=canonical, cycle_time=cycle_time, model_id=f"model_{index}"
                )
                decision = _state_decision(repository, source, index)
                assert (decision.action, decision.reason) == ("skip", "active_duplicate_pipeline")
                assert decision.evidence["decision"] == "skip_active"

        rows = _array_rows(
            "56823", submit=IFS_ARRAY_SUBMIT, submit_line=_submit_line(IFS_KEY), tasks=members
        ) + _array_rows("56839", submit=GFS_ARRAY_SUBMIT, submit_line=_submit_line(GFS_KEY), tasks=members)
        query, commands = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        bound = reconcile_reserved_unbound_jobs(repository, comment_query=query, now=lambda: QUERY_END)
        assert {(outcome.job_id, outcome.action, outcome.slurm_job_id) for outcome in bound} == {
            (GFS_JOB, "bound", "56839"),
            (IFS_JOB, "bound", "56823"),
        }

        def _master(master_id: str) -> SacctRecord:
            tasks = tuple(
                SacctRecord(
                    f"{master_id}_{index}",
                    "COMPLETED",
                    "nhms_forecast",
                    exit_code="0:0",
                    user="scheduler",
                    account="account",
                    array_task_id=index,
                )
                for index in range(members)
            )
            return SacctRecord(
                slurm_job_id=master_id,
                raw_state="COMPLETED",
                job_name="nhms_forecast",
                exit_code="0:0",
                comment=None,
                user="scheduler",
                account="account",
                array_member_job_ids=tuple(task.slurm_job_id for task in tasks),
                array_task_records=tasks,
            )

        masters = {"56823": _master("56823"), "56839": _master("56839")}
        queried: list[str] = []

        def _sacct(job_id: str) -> SacctRecord | None:
            queried.append(str(job_id))
            return masters.get(str(job_id))

        inflight = reconcile_inflight_jobs(repository, sacct_query=_sacct)

        assert {(outcome.job_id, outcome.action, outcome.status) for outcome in inflight} == {
            (GFS_JOB, "terminal", "succeeded"),
            (IFS_JOB, "terminal", "succeeded"),
        }
        assert set(queried) <= {"56823", "56839"}
        for source, canonical in (("gfs", "gfs"), ("ifs", "IFS")):
            for index in range(members):
                assert not repository.has_active_pipeline(
                    source_id=canonical, cycle_time=cycle_time, model_id=f"model_{index}"
                )
                hydro = repository._hydro_run_for(f"fcst_{source}_2026071200_model_{index}")
                assert hydro["status"] == "succeeded"
                decision = _state_decision(repository, source, index)
                # The member resumes AFTER the completed forecast stage: the
                # finished SHUD forecast is never re-run.
                assert (decision.action, decision.reason) == ("retry", "resume_after_completed_stage")
                assert decision.evidence["restart_stage"] == "state_save_qc"
                assert decision.evidence["native_shud_resubmitted"] is False
        # ``commands`` only records what the monkeypatched fallback querier ran
        # (it is not an sbatch interceptor): the bind pass issued sacct reads
        # only. The no-resubmission proof is the resume decision above.
        assert commands
        assert all(Path(command[0]).name == "sacct" for command in commands)


def test_compact_restart_reconcile_evidence_keeps_the_match_basis() -> None:
    """Under evidence pressure the compact outcome projection keeps the
    additive basis on fallback rows and never fabricates it elsewhere."""

    from services.orchestrator.scheduler_evidence_payload import _compact_bounded_reconcile_lane

    compact = _compact_bounded_reconcile_lane(
        {
            "outcomes": [
                {
                    "job_id": GFS_JOB,
                    "idempotency_key": GFS_KEY,
                    "action": "bound",
                    "status": "submitted",
                    "slurm_binding_source": "slurm_name_window_unique",
                    "fallback_match_basis": "submitline_exact",
                },
                {"job_id": IFS_JOB, "action": "query_unavailable", "status": "reserved"},
            ]
        }
    )

    assert compact == [
        {
            "job_id": GFS_JOB,
            "action": "bound",
            "status": "submitted",
            "slurm_binding_source": "slurm_name_window_unique",
            "fallback_match_basis": "submitline_exact",
        },
        {"job_id": IFS_JOB, "action": "query_unavailable", "status": "reserved"},
    ]
