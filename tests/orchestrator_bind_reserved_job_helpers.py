"""Shared fixtures/helpers for the #2668 bind-reserved-job suites.

Not a collectible test module (pytest's ``python_files`` ignores this name).

Every held row is produced by the REAL restart-reconcile producer
(``reconcile_reserved_unbound_jobs``) over fake ``sacct`` stdout, never a
hand-written post-state:

* shape (a): two in-window ``nhms_forecast`` masters without a SubmitLine ->
  ``ambiguous_fallback_match`` / basis ``name_window_count``;
* shape (b): the comment-less fallback window saturates its scan budget ->
  ``query_unavailable`` (pass reason ``bounded_output_rows``).

Both leave the durable held tuple ``slurm_exact_comment`` /
``accounting_unavailable`` / ``comment_accounting_unproven``
(``reconcile.py`` ``_record_file_reconciliation``), which is what the operator
bind accepts.  Timestamps follow the node-22 production shape of #2655: the
fixture anchor is ``2026-07-12T00:00:13Z`` and the arrays are accepted about two
minutes later.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
from tests.gateway_reconcile_helpers import _file_cohort_repository
from tests.test_gateway_reconcile_claimant_exclusivity import _fallback_querier, _fallback_row

JOB_ID = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
KEY = "cycle_gfs_2026071200_forecast_fixture:forecast"
OWN_COMMENT = f"nhms_idem:{KEY}"
FOREIGN_KEY = "cycle_ifs_2026071200_forecast_fixture:forecast"
ANCHOR = datetime(2026, 7, 12, 0, 0, 13, tzinfo=UTC)
QUERY_END = datetime(2026, 7, 12, 2, 0, 0, tzinfo=UTC)
MASTER_ID = "56839"
OTHER_MASTER_ID = "56840"
#: sacct ``Submit`` of the verified master, local naive in sacct (TZ pinned to UTC).
MASTER_SUBMIT_LOCAL = "2026-07-12T00:02:23"
MASTER_SUBMIT = "2026-07-12T00:02:23Z"
CHECKED_AT = "2026-07-12T02:30:00Z"
SUBMIT_LINE = f"/usr/bin/sbatch --array=0-0%15 --comment={OWN_COMMENT} /tmp/nhms_6y2bx17b.sbatch"
NOTE = (
    "sacct -j 56839 --format=JobID,JobName,State,Submit,SubmitLine: COMPLETED, "
    "Submit 2026-07-12T00:02:23, SubmitLine carries this key; 56840 is the IFS array"
)
#: The exact durable tuple the #2655 automatic name-window bind writes.
BIND_TUPLE_FIELDS = (
    "status",
    "slurm_job_id",
    "submit_outcome",
    "reconciliation_source",
    "reconciliation_decision",
    "reconciliation_reason_class",
    "matched_slurm_job_id",
    "slurm_binding_source",
    "slurm_accounting_submitted_at",
    "submission_attempt",
    "submission_attempt_started_at",
)


def _fallback_array(master_id: str, *, submit: str, submit_line: str | None) -> str:
    return _fallback_row(f"{master_id}_0", submit=submit, submit_line=submit_line)


def held_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str = "a") -> Any:
    """One current-contract forecast master held by the real reconcile producer."""

    from services.orchestrator import reconcile as reconcile_module

    repository = _file_cohort_repository(
        tmp_path / f"held-{shape}",
        created_at=ANCHOR,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
    )
    if shape == "a":
        rows = _fallback_array(MASTER_ID, submit=MASTER_SUBMIT_LOCAL, submit_line=None) + _fallback_array(
            OTHER_MASTER_ID, submit="2026-07-12T00:02:17", submit_line=None
        )
        query, _commands = _fallback_querier(monkeypatch, rows=rows, query_end=QUERY_END)
        expected_action = "ambiguous_fallback_match"
    elif shape == "b":

        def _saturated(_command: Any, **_kwargs: Any) -> str:
            raise reconcile_module.ReconcileQuerySaturated("rows")

        monkeypatch.setattr(reconcile_module, "_bounded_sacct_stdout", _saturated)
        query = reconcile_module.default_comment_sacct_querier(
            global_visibility_probe=lambda: True,
            comment_storage_probe=lambda: False,
            now=lambda: QUERY_END,
        )
        expected_action = "query_unavailable"
    else:
        raise AssertionError(f"unknown shape {shape}")
    (outcome,) = reconcile_module.reconcile_reserved_unbound_jobs(
        repository, comment_query=query, now=lambda: QUERY_END
    )
    assert outcome.action == expected_action
    if shape == "a":
        assert outcome.fallback_match_basis == "name_window_count"
    held = repository.get_accepted_submit_pipeline_job(JOB_ID)
    assert (held["status"], held["slurm_job_id"], held["matched_slurm_job_id"]) == ("reserved", None, None)
    assert (
        held["submit_outcome"],
        held["reconciliation_source"],
        held["reconciliation_decision"],
        held["reconciliation_reason_class"],
    ) == ("submit_result_ambiguous", "slurm_exact_comment", "accounting_unavailable", "comment_accounting_unproven")
    return repository


def held_row(repository: Any) -> dict[str, Any]:
    return repository.get_accepted_submit_pipeline_job(JOB_ID)


def bind_kwargs(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(row["submission_attempt"]),
        "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
        "slurm_job_id": MASTER_ID,
        "slurm_submit_time": MASTER_SUBMIT,
        "submit_line": SUBMIT_LINE,
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": NOTE,
    }
    kwargs.update(overrides)
    return kwargs


def cli_args(root: Path, row: dict[str, Any], *, confirm: bool = True, **overrides: str) -> list[str]:
    values = {
        "--journal-root": str(root),
        "--job-id": JOB_ID,
        "--slurm-job-id": MASTER_ID,
        "--slurm-submit-time": MASTER_SUBMIT,
        "--submit-line": SUBMIT_LINE,
        "--expected-attempt": str(row["submission_attempt"]),
        "--expected-attempt-started-at": str(row["submission_attempt_started_at"]),
        "--checked-by": "operator-alice",
        "--checked-at": CHECKED_AT,
        "--verification-note": NOTE,
    }
    values.update({key: value for key, value in overrides.items()})
    args = ["bind-reserved-job"]
    for option, value in values.items():
        args.extend([option, value])
    if confirm:
        args.append("--confirm")
    return args


def journal_bytes(root: Path) -> dict[str, bytes]:
    """Every durable byte under the journal root (lock files are not state)."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".locks" not in path.parts
    }


def bind_events(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("record_type") == "pipeline_event" and payload.get("event_type") == "operator_verified_bind":
                events.append(payload)
    return events


def journal_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def after(value: str, **delta: float) -> str:
    """``value`` (an ISO ``...Z`` instant) shifted by ``timedelta(**delta)``, as ``...Z``."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(**delta)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
