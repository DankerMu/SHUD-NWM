"""Shared fixtures/helpers for the split #1564 demote-reserved-job suites.

Not a collectible test module (pytest's ``python_files`` ignores this name);
the four ``tests/test_orchestrator_demote_*.py`` suites import their fixtures
from here so no helper is duplicated across the split.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from services.orchestrator import reconcile as reconcile_module
from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION
from tests.test_gateway_reconcile import _file_cohort_repository

JOB_ID = "job_cycle_gfs_2026071200_forecast_fixture_forecast"
STARTED_AT = datetime(2026, 7, 12, tzinfo=UTC)


def _held_cohort_repository(
    tmp_path: Path,
    *,
    member_count: int = 2,
    source_id: str = "gfs",
    active_hydro: bool = False,
) -> Any:
    """One versioned reserved-unbound master held by the #1116 comment-less gate.

    ``reconcile_reserved_unbound_jobs`` is the real producer: with no comment
    storage proof the query raises ``ReconcileQueryUnavailable`` and the pass
    records ``accounting_unavailable`` / ``comment_accounting_unproven`` while
    keeping the row ``reserved``.  This is exactly the row the operator command
    may demote.  ``active_hydro`` additionally books each member's hydro row in
    an ACTIVE status so the demotion's member fan-out has rows to project.
    """

    # Runtime rows give the cohort the identity the accepted-submit comment gate
    # needs, so reconcile routes the row through the comment query that records
    # the held accounting_unavailable/comment_accounting_unproven shape.
    repository = _file_cohort_repository(
        tmp_path,
        created_at=STARTED_AT,
        member_count=member_count,
        source_id=source_id,
        with_runtime_rows=True,
    )
    if active_hydro:
        # The runtime placeholder rows land as ``failed``; book each member's
        # hydro row into an ACTIVE status so the demotion fan-out has targets.
        from packages.common.source_identity import normalize_source_id

        canonical_source = normalize_source_id(source_id)
        for index in range(member_count):
            repository.update_hydro_run_status(
                f"fcst_{canonical_source.lower()}_2026071200_model_{index}",
                "running",
            )
    query_end = STARTED_AT + timedelta(hours=1)

    class _NoCommentQuery:
        def __call__(self, _key: str, **kwargs: Any) -> Any:
            del kwargs
            raise reconcile_module.ReconcileQueryUnavailable(
                "accounting does not store job comments",
                reason_class="comment_accounting_unproven",
            )

    outcomes = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_NoCommentQuery(),
        grace=timedelta(0),
        now=lambda: query_end,
    )
    assert [outcome.action for outcome in outcomes] == ["query_unavailable"]
    held = repository.get_accepted_submit_pipeline_job(JOB_ID)
    assert held["status"] == "reserved"
    assert held["slurm_job_id"] is None
    assert held["reconciliation_decision"] == "accounting_unavailable"
    assert held["reconciliation_reason_class"] == "comment_accounting_unproven"
    return repository


def _production_faithful_held_cohort_repository(
    tmp_path: Path,
    *,
    member_count: int = 2,
    source_id: str = "gfs",
    active_hydro: bool = False,
) -> Any:
    """#1564 production-faithful held row for the public operator-recovery tests.

    Production's ``_reserve_cycle_stage`` stamps the master with
    ``candidate_id=run_id`` and ``native_shud_resubmitted=True``, while the
    shared ``_file_cohort_repository`` clean-reservation payload carries neither
    (both stay ``None``).  A public ``orchestrate_cycle`` reclaim compares the
    full immutable master identity, so only this variant can exercise the real
    recovery path.  It reuses the gateway suite's private
    ``_versioned_master_reservation_record`` / ``_append_cohort_placeholders``,
    fills the two production fields before the first reserve, recomputes
    ``cohort_digest``, then runs the real repository reserve -> timeout
    transition -> runtime placeholders -> #1116 reconcile producer chain.
    """
    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
        forecast_cohort_digest,
    )
    from services.orchestrator.file_orchestration_journal import (
        FileOrchestrationJournalRepository,
    )
    from tests.test_gateway_reconcile import (
        _append_cohort_placeholders,
        _versioned_master_reservation_record,
    )

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    canonical_source = normalize_source_id(source_id).lower()
    record = _versioned_master_reservation_record(
        created_at=STARTED_AT,
        member_count=member_count,
        source_id=canonical_source,
    )
    record["candidate_id"] = record["run_id"]
    record["native_shud_resubmitted"] = True
    record["cohort_digest"] = forecast_cohort_digest(record)
    repository.reserve_pipeline_job(record)
    repository.transition_pipeline_job_submit_evidence(
        record["job_id"],
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    _append_cohort_placeholders(repository, member_count, source_id=canonical_source)
    if active_hydro:
        for index in range(member_count):
            repository.update_hydro_run_status(
                f"fcst_{canonical_source}_2026071200_model_{index}",
                "running",
            )
    query_end = STARTED_AT + timedelta(hours=1)

    class _NoCommentQuery:
        def __call__(self, _key: str, **kwargs: Any) -> Any:
            del kwargs
            raise reconcile_module.ReconcileQueryUnavailable(
                "accounting does not store job comments",
                reason_class="comment_accounting_unproven",
            )

    outcomes = reconcile_module.reconcile_reserved_unbound_jobs(
        repository,
        comment_query=_NoCommentQuery(),
        grace=timedelta(0),
        now=lambda: query_end,
    )
    assert [outcome.action for outcome in outcomes] == ["query_unavailable"]
    held = repository.get_accepted_submit_pipeline_job(JOB_ID)
    assert held["status"] == "reserved"
    assert held["slurm_job_id"] is None
    assert held["reconciliation_decision"] == "accounting_unavailable"
    assert held["reconciliation_reason_class"] == "comment_accounting_unproven"
    assert held["candidate_id"] == record["run_id"]
    assert held["native_shud_resubmitted"] is True
    return repository


def _journal_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".locks" not in path.parts
    }


def _held_row(repository: Any) -> dict[str, Any]:
    return repository.get_accepted_submit_pipeline_job(JOB_ID)


def _demote_kwargs(row: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(row["submission_attempt"]),
        "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
        "checked_by": "operator-alice",
        "checked_at": STARTED_AT + timedelta(hours=2),
        "verification_note": "sacct and squeue show no matching nhms_forecast job in the attempt window",
    }
    kwargs.update(overrides)
    return kwargs


def _durable_event_payloads(root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("record_type") == "pipeline_event" and payload.get("entity_id") == JOB_ID:
                events.append(payload)
    return events


def _durable_hydro_payloads(root: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("record_type") == "hydro_run":
                payloads.append(payload)
    return payloads


def _absence_row(repository: Any, *, decision: str) -> dict[str, Any]:
    row = _held_row(repository)
    assert repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(row),
    ) is not None
    return _held_row(repository)


def _cli_base_args(repository: Any, row: dict[str, Any]) -> list[str]:
    return [
        "demote-reserved-job",
        "--journal-root",
        str(repository.root),
        "--job-id",
        JOB_ID,
        "--expected-attempt",
        str(row["submission_attempt"]),
        "--expected-attempt-started-at",
        str(row["submission_attempt_started_at"]),
        "--checked-by",
        "operator-alice",
        "--checked-at",
        (STARTED_AT + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "--verification-note",
        "sacct and squeue show no matching nhms_forecast job in the attempt window",
    ]


SECRET_CHECKED_BY = (
    "operator-alice password=supersecret "
    "postgresql://nwm:dbsecret@db.example/nhms"
)
SECRET_VERIFICATION_NOTE = (
    "sacct shows dead job; auth Authorization: Bearer abc.def.ghi, "
    "Basic dXNlcjpwYXNz, token=tok_live_123, password=p@ss, signed "
    "https://minio.example/bucket/obj?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIAEXAMPLE"
)
SECRET_LITERALS = (
    "supersecret",
    "dbsecret",
    "abc.def.ghi",
    "dXNlcjpwYXNz",
    "tok_live_123",
    "p@ss",
    "deadbeef",
    "AKIAEXAMPLE",
)


def _cli_args_with_secrets(repository: Any, row: dict[str, Any]) -> list[str]:
    args = _cli_base_args(repository, row)
    args[args.index("--checked-by") + 1] = SECRET_CHECKED_BY
    args[args.index("--verification-note") + 1] = SECRET_VERIFICATION_NOTE
    return args


def _durable_bytes(root: Path) -> bytes:
    chunks: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".locks" not in path.parts:
            chunks.append(path.read_bytes())
    return b"\n".join(chunks)


PATH_SECRET_CHECKED_BY = "operator-alice /home/frd/private/proof"
PATH_SECRET_VERIFICATION_NOTE = (
    "checked /home/frd/private/proof and /tmp/secret.json; "
    "auth Authorization: Bearer abc.def.ghi; "
    "signed https://minio.example/bucket/obj?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIAEXAMPLE"
)
PATH_SECRET_LITERALS = (
    "/home/frd/private/proof",
    "/tmp/secret.json",
    "abc.def.ghi",
    "minio.example",
    "/bucket/obj",
    "deadbeef",
    "AKIAEXAMPLE",
)


def _axis_repository(tmp_path: Path, mutator: str) -> Any:
    """One repository whose master differs from the held shape on one CAS axis.

    Every variant is produced by a legitimate typed transition or the real
    reconcile producer -- never a hand-edited post-state -- so each proves the
    compare-and-swap reads the persisted mismatch and refuses with zero bytes.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
    from services.orchestrator.reconcile import (
        ReconcileQueryUnavailable,
        SacctRecord,
        reconcile_reserved_unbound_jobs,
    )

    if mutator == "legacy":
        # Explicit legacy (non-current contract) row, structurally a master.
        return _file_cohort_repository(tmp_path, member_count=2, versioned=False)
    if mutator == "pre_decision":
        # reserved + timeout only: submit_result_ambiguous with a null
        # reconciliation tuple (source None instead of slurm_exact_comment).
        return _file_cohort_repository(tmp_path, member_count=2)
    repository = _file_cohort_repository(tmp_path, member_count=2)
    row = repository.get_accepted_submit_pipeline_job(JOB_ID)
    idempotency_key = str(row["idempotency_key"])
    if mutator in {"running", "submitted", "pending"}:
        result = repository.commit_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=JOB_ID,
            expected_submission_attempt=1,
            slurm_job_id="17667",
            transition=AcceptedSubmitTransition.accepted(status="submitted"),
        )
        assert result.committed
        if mutator != "submitted":
            assert repository.transition_pipeline_job_runtime_status(
                JOB_ID,
                mutator,
                expected_statuses=("submitted",),
            ).wrote
        return repository
    if mutator in {"submission_failed", "rejected"}:
        from datetime import timedelta

        assert repository.reject_pipeline_job_submit_attempt(
            idempotency_key,
            pipeline_job_id=JOB_ID,
            expected_submission_attempt=1,
            finished_at=STARTED_AT + timedelta(hours=2),
            error_code="SBATCH_REJECTED",
            error_message="queue policy",
            stage="forecast",
            job_type="run_shud_forecast_array",
        ).committed
        return repository
    key = "cycle_gfs_2026071200_forecast_fixture:forecast"
    exact = SacctRecord(
        slurm_job_id="17667",
        raw_state="RUNNING",
        job_name="nhms_forecast",
        comment=f"nhms_idem:{key}",
        run_id="cycle_gfs_2026071200_forecast_fixture",
        stage="forecast",
        pipeline_job_id=JOB_ID,
    )
    if mutator == "matched":
        outcome = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _k: exact)[0]
        assert outcome.reconciliation_decision == "matched_bound"
        return repository
    if mutator == "identity_mismatch":
        wrong = SacctRecord(**{**exact.__dict__, "stage": "forcing"})
        outcome = reconcile_reserved_unbound_jobs(repository, comment_query=lambda _k: wrong)[0]
        assert outcome.reconciliation_decision == "identity_mismatch_blocked"
        return repository
    if mutator == "process_unavailable":

        def unavailable_query(_key: str) -> None:
            raise ReconcileQueryUnavailable("process unavailable", reason_class="process_unavailable")

        outcome = reconcile_reserved_unbound_jobs(repository, comment_query=unavailable_query)[0]
        assert outcome.reconciliation_decision == "accounting_unavailable"
        assert outcome.reconciliation_reason_class == "process_unavailable"
        return repository
    raise AssertionError(f"unknown axis mutator: {mutator}")


def _axis_mismatch_field(repository: Any, mutator: str) -> tuple[str, object]:
    row = repository.get_accepted_submit_pipeline_job(JOB_ID)
    return {
        "running": ("status", row["status"]),
        "submitted": ("status", row["status"]),
        "pending": ("status", row["status"]),
        "submission_failed": ("status", row["status"]),
        "rejected": ("submit_outcome", row["submit_outcome"]),
        "matched": ("matched_slurm_job_id", row["matched_slurm_job_id"]),
        "pre_decision": ("reconciliation_source", row["reconciliation_source"]),
        "identity_mismatch": ("reconciliation_decision", row["reconciliation_decision"]),
        "process_unavailable": ("reconciliation_reason_class", row["reconciliation_reason_class"]),
        "legacy": ("accepted_submit_contract_version", row.get("accepted_submit_contract_version")),
    }[mutator]
