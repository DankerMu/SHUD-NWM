"""Coupling pin for the file journal's read-blocked sentinel and its retry-lane consumers.

`_blocked_query_job` is the ONE synthetic row the journal's five query
entrypoints return when a read is refused: PRESENT, non-terminal, marked by
``file_journal = {"status": "blocked", ...}``.  #2385 (the manual-retry source
selector) and #2387 (the chain stage-result retry classifier and the two
runtime-root provenance readers) are two consumer ends of that ONE family, so
they live in one module against ONE discriminator: a later producer-side change
must not be able to satisfy one end and silently break the other.

Governing invariant (design D1): a refused journal read is never reported as an
answer about the work.  Each consumer owes the row exactly one of two
treatments -- refuse with the lane's classified error carrying the journal
``reason``/``field``, or degrade to the lane's "no evidence" value and log that
``reason``/``field``.

Every import is function-local on purpose.  ``scripts/select_ci_tests`` derives
its importer index from module-level statements only, so a top-level journal or
chain import here would enlarge that index; this module is routed by explicit
rules instead (see ``tests/test_select_ci_tests.py``).
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_RUN_ID = "fcst_gfs_2026072000_model_a"
_CYCLE_ID = "gfs_2026072000"
_CYCLE_ISO = "2026-07-20T00:00:00+00:00"
_JOB_ID = "job_fcst_gfs_2026072000_model_a_forecast"
_JOURNAL_LOGGER = "services.orchestrator.file_orchestration_journal"

# The motivating refusal: the whole-tree replay budget (#1953).  Reason and
# field are journal tokens, and every classified refusal below must carry these
# exact two rather than a derived identity fault of the consumer's own making.
_BLOCK_REASON = "file_journal_record_limit_exceeded"
_BLOCK_FIELD = "pipeline_job_records"
_BLOCK_EVIDENCE = {"lane": "full_tree_replay"}


def _journal() -> Any:
    import services.orchestrator.file_orchestration_journal as module

    return module


def _blocked_fault() -> Any:
    journal = _journal()
    return journal.FileOrchestrationJournalError(_BLOCK_REASON, field=_BLOCK_FIELD, evidence=_BLOCK_EVIDENCE)


def _pipeline_job_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "job_id": _JOB_ID,
        "run_id": _RUN_ID,
        "cycle_id": _CYCLE_ID,
        "source_id": "gfs",
        "cycle_time": _CYCLE_ISO,
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "failed",
        "stage": "forecast",
        "idempotency_key": "gfs:gfs_2026072000:model_a:forecast",
        "error_code": "SLURM_TIMEOUT",
        "init_state_identities": [],
        "created_at": _CYCLE_ISO,
        "updated_at": _CYCLE_ISO,
        "finished_at": _CYCLE_ISO,
    }
    record.update(overrides)
    return record


def _seeded_journal(tmp_path: Path, *, jobs: list[dict[str, Any]] | None = None) -> tuple[Path, Any, Any]:
    """A real journal tree plus a real file-lane retry service over it."""

    journal = _journal()
    from services.orchestrator.retry import RetryConfig

    # ``.resolve()``: the repository refuses a root reached through a symlinked
    # ancestor, and ``tmp_path`` is a symlink under ``/var`` on macOS.
    root = (tmp_path / "journal").resolve()
    repository = journal.FileOrchestrationJournalRepository(root)
    for job in jobs if jobs is not None else [_pipeline_job_record()]:
        repository.upsert_pipeline_job(job)
    service = journal.FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    return root, repository, service


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every durable byte under the journal root; flock files are not records."""

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".lock"
    }


def _journal_records(root: Path, record_type: str) -> list[dict[str, Any]]:
    """Append-only records read straight off disk, never through the reader under test."""

    records: list[dict[str, Any]] = []
    for segment in sorted(root.rglob("*.jsonl")):
        for line in segment.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != record_type:
                continue
            records.append(record.get("payload") or {})
    return records


def _refuse_scoped_job_records(monkeypatch: pytest.MonkeyPatch, repository: Any) -> None:
    """The seam inside ``query_pipeline_jobs_by_run``'s ``try`` (#2385 by-run lane)."""

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise _blocked_fault()

    monkeypatch.setattr(repository, "_iter_pipeline_job_records_scoped", _refuse)


def _refuse_job_id_reads(monkeypatch: pytest.MonkeyPatch, repository: Any) -> None:
    """The seam inside ``get_pipeline_job``'s ``try`` (#2387 by-id lane)."""

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise _blocked_fault()

    monkeypatch.setattr(repository, "_pipeline_job_for_id_unlocked", _refuse)


class _UnreachableGateway:
    """Any gateway call at all is a zero-write violation on a refusal path."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def submit_job(self, request: Any) -> Any:
        self.requests.append(request)
        raise AssertionError("a refused journal read must not reach the Slurm gateway")


class _SuccessGateway:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def submit_job(self, request: Any) -> Any:
        self.requests.append(request)
        return {"job_id": "551234", "status": "submitted"}


# --- 1. the discriminator (design D1) -------------------------------------


def test_discriminator_is_true_for_every_blocked_lane_and_false_for_a_real_row() -> None:
    """Task 1.3: shape-keyed on the marker, across all five producer lanes.

    No identity field discriminates all five: the by-run/by-cycle lanes keep the
    real ``run_id``/``cycle_id`` and the DEFAULT ``job_id``, while the by-id lane
    keeps the real ``job_id``.  Only ``file_journal.status == "blocked"`` covers
    every lane, which is why the ``job_id`` comparison is deleted rather than
    kept as belt-and-braces.
    """

    journal = _journal()
    error = _blocked_fault()
    rows = {
        "idempotency_key": journal._blocked_query_job(error, idempotency_key="gfs:gfs_2026072000:model_a:forecast"),
        "job_id": journal._blocked_query_job(error, job_id=_JOB_ID),
        "cycle_id": journal._blocked_query_job(error, cycle_id=_CYCLE_ID),
        "run_id": journal._blocked_query_job(error, run_id=_RUN_ID),
        "slurm_job_id": journal._blocked_query_job(error, slurm_job_id="551234"),
    }
    for lane, row in rows.items():
        assert journal._is_blocked_query_job(row) is True, lane
        assert journal._blocked_query_job_fault(row) == (_BLOCK_REASON, _BLOCK_FIELD), lane

    assert journal._is_blocked_query_job(_pipeline_job_record()) is False
    # The chain classifier duck-types ``repository.jobs``: a plain mapping with
    # no marker must read as a normal row, not as blocked.
    assert journal._is_blocked_query_job({}) is False
    assert journal._is_blocked_query_job({"status": "failed"}) is False
    assert journal._is_blocked_query_job({"file_journal": {"status": "ok"}}) is False
    assert journal._is_blocked_query_job({"file_journal": "blocked"}) is False
    assert journal._is_blocked_query_job(None) is False


def test_blocked_row_shape_is_pinned_field_by_field_across_the_five_lanes() -> None:
    """Task 4.1: the producer row both consumer ends are keyed on.

    Field set, ``job_id`` defaults, marker keys, reason token and the
    non-terminal status literal are #1953's contract (this change's non-goal).
    Pinned here so a producer-side edit cannot satisfy one consumer end and
    silently break the other.
    """

    journal = _journal()
    error = _blocked_fault()
    expected_common = {
        "slurm_job_id": "unknown_after_attempt",
        "status": journal.FILE_JOURNAL_READ_BLOCKED_STATUS,
        "stage": "file_journal_read",
        "error_code": _BLOCK_REASON,
        "file_journal": {
            "status": "blocked",
            "reason": _BLOCK_REASON,
            "field": _BLOCK_FIELD,
            "evidence": dict(_BLOCK_EVIDENCE),
        },
    }
    lanes = [
        ({"idempotency_key": "gfs:gfs_2026072000:model_a:forecast"}, "file_journal_read_blocked", None, None),
        ({"job_id": _JOB_ID}, _JOB_ID, None, None),
        ({"cycle_id": _CYCLE_ID}, "file_journal_read_blocked", _CYCLE_ID, None),
        ({"run_id": _RUN_ID}, "file_journal_read_blocked", None, _RUN_ID),
        ({"slurm_job_id": "551234"}, "file_journal_read_blocked", None, None),
    ]
    for kwargs, expected_job_id, expected_cycle_id, expected_run_id in lanes:
        row = journal._blocked_query_job(error, **kwargs)
        assert set(row) == {
            "job_id",
            "idempotency_key",
            "cycle_id",
            "run_id",
            "slurm_job_id",
            "status",
            "stage",
            "error_code",
            "file_journal",
        }, kwargs
        assert row["job_id"] == expected_job_id, kwargs
        assert row["cycle_id"] == expected_cycle_id, kwargs
        assert row["run_id"] == expected_run_id, kwargs
        assert row["idempotency_key"] == kwargs.get("idempotency_key"), kwargs
        for key, value in expected_common.items():
            if key == "slurm_job_id" and "slurm_job_id" in kwargs:
                assert row[key] == kwargs["slurm_job_id"], kwargs
                continue
            assert row[key] == value, (kwargs, key)

    # Non-terminal and PRESENT: the duplicate-submission and active-cycle guards
    # read the row as in-flight, which is what keeps them refusing.
    assert journal.FILE_JOURNAL_READ_BLOCKED_STATUS not in journal.TERMINAL_PIPELINE_STATUSES
    assert journal.FILE_JOURNAL_READ_BLOCKED_STATUS != "running"


def test_journal_import_does_not_load_the_chain_execution_module() -> None:
    """Task 1.2: the chain imports the journal, never the reverse -- no new cycle."""

    probe = (
        "import sys;"
        "import services.orchestrator.file_orchestration_journal;"
        "print('services.orchestrator.chain_forecast_execution' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "False"

    # And the chain's static compat inventory still tracks the classifier as a
    # chain-local bridge symbol: the guard is added inside the existing function,
    # never by renaming or moving it.  ``chain`` first: ``chain_compat_static``
    # is imported BY it and is only fully initialised through that entrypoint.
    import services.orchestrator.chain  # noqa: F401
    from services.orchestrator import chain_compat_static

    assert "_retry_job_for_stage_result" in chain_compat_static._CHAIN_RETRY_COMPAT_LOCAL_METHOD_NAMES


def test_both_consumer_ends_key_on_the_single_discriminator_definition() -> None:
    """Task 4.1: one definition, two consumers -- no second drifting local copy."""

    journal = _journal()
    from services.orchestrator import chain_forecast_execution

    assert chain_forecast_execution._is_blocked_query_job is journal._is_blocked_query_job
    assert chain_forecast_execution._blocked_query_job_fault is journal._blocked_query_job_fault

    selector_source = inspect.getsource(journal.FileJournalRetryService._manual_retry_source_for_run)
    classifier_source = inspect.getsource(chain_forecast_execution._retry_job_for_stage_result)
    for source in (selector_source, classifier_source):
        assert "_is_blocked_query_job(" in source
        # The deleted discriminator must not reappear in either consumer.
        assert "file_journal_read_blocked" not in source
        assert '"blocked"' not in source


# --- 2. #2385 -- the manual-retry source selector (design D2) --------------


def test_blocked_by_run_read_refuses_with_retry_evidence_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.1: a refused read is not "this run has nothing to retry"."""

    journal = _journal()
    _root, repository, service = _seeded_journal(tmp_path)
    _refuse_scoped_job_records(monkeypatch, repository)

    with pytest.raises(journal.RetryEvidenceInvalidError) as pending_error:
        service._create_pending_manual_retry_job(_RUN_ID)
    assert pending_error.value.details["run_id"] == _RUN_ID
    assert pending_error.value.details["journal_reason"] == _BLOCK_REASON
    assert pending_error.value.details["journal_field"] == _BLOCK_FIELD

    with pytest.raises(journal.RetryEvidenceInvalidError) as attempt_error:
        service.attempt_manual_retry(_RUN_ID, _UnreachableGateway(), trusted_internal=True)
    assert attempt_error.value.code == "RETRY_EVIDENCE_INVALID"
    assert attempt_error.value.status_code == 409
    assert attempt_error.value.details["journal_reason"] == _BLOCK_REASON
    assert attempt_error.value.details["journal_field"] == _BLOCK_FIELD


def test_blocked_by_run_read_writes_nothing_and_never_allocates_a_retry_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tasks 2.3 and 2.6's zero-write half, plus 3.6's allocator-unreachable pin."""

    journal = _journal()
    root, repository, service = _seeded_journal(tmp_path)
    before = _tree_bytes(root)
    assert before, "the fixture must have seeded durable bytes for the diff to mean anything"

    def _unreachable_allocator(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("the selector refuses first: the retry-id allocator is unreachable")

    monkeypatch.setattr(journal, "_next_file_manual_retry_job_id_for_run", _unreachable_allocator)
    _refuse_scoped_job_records(monkeypatch, repository)
    gateway = _UnreachableGateway()

    with pytest.raises(journal.RetryEvidenceInvalidError):
        service.attempt_manual_retry(_RUN_ID, gateway, trusted_internal=True)

    assert gateway.requests == []
    assert _tree_bytes(root) == before
    assert [
        record
        for record in _journal_records(root, "pipeline_event")
        if (record.get("details") or {}).get("manual_retry_marker") is True
    ] == []


def test_refused_durable_run_read_takes_the_same_classified_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.2: ``_hydro_run_for`` has no ``except`` today, so a fault escapes untyped."""

    journal = _journal()
    _root, repository, service = _seeded_journal(tmp_path)

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise journal.FileOrchestrationJournalError("file_journal_unsafe_identity", field="run_id")

    monkeypatch.setattr(repository, "_hydro_run_for", _refuse)

    with pytest.raises(journal.RetryEvidenceInvalidError) as error:
        service.attempt_manual_retry(_RUN_ID, _UnreachableGateway(), trusted_internal=True)
    assert error.value.details["journal_reason"] == "file_journal_unsafe_identity"
    assert error.value.details["journal_field"] == "run_id"


def test_absence_conflict_and_selection_stay_three_distinguishable_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.5: "nothing to retry", "busy" and "unreadable" must not collapse."""

    journal = _journal()

    _root, _repository, failed_service = _seeded_journal(tmp_path / "failed")
    selected, active = failed_service._manual_retry_source_for_run(_RUN_ID)
    assert active is None
    assert selected is not None and selected["job_id"] == _JOB_ID

    _root, _repository, empty_service = _seeded_journal(
        tmp_path / "empty",
        jobs=[
            _pipeline_job_record(
                job_id="job_fcst_gfs_2026072000_model_b_forecast",
                run_id="fcst_gfs_2026072000_model_b",
                model_id="model_b",
            )
        ],
    )
    assert empty_service._manual_retry_source_for_run(_RUN_ID) == (None, None)
    with pytest.raises(journal.RetryNotFoundError):
        empty_service.attempt_manual_retry(_RUN_ID, _UnreachableGateway(), trusted_internal=True)

    _root, _repository, active_service = _seeded_journal(
        tmp_path / "active", jobs=[_pipeline_job_record(status="running", finished_at=None, error_code=None)]
    )
    _selected, active_job = active_service._manual_retry_source_for_run(_RUN_ID)
    assert active_job is not None and active_job["job_id"] == _JOB_ID
    with pytest.raises(journal.RetryConflictError):
        active_service.attempt_manual_retry(_RUN_ID, _UnreachableGateway(), trusted_internal=True)

    # A durable-success run still answers "nothing to retry", unchanged.
    _root, success_repository, success_service = _seeded_journal(
        tmp_path / "succeeded", jobs=[_pipeline_job_record(status="succeeded", error_code=None)]
    )
    monkeypatch.setattr(
        success_repository,
        "_hydro_run_for",
        lambda run_id: {"run_id": run_id, "status": sorted(journal.MANUAL_RETRY_DURABLE_SUCCESS_STATUSES)[0]},
    )
    assert success_service._manual_retry_source_for_run(_RUN_ID) == (None, None)


def test_route_answers_409_retry_evidence_invalid_for_a_blocked_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.6: the operator gets a classified 409, not a 404 and not a traceback."""

    from fastapi.testclient import TestClient

    from apps.api.main import app
    from apps.api.routes import pipeline as pipeline_routes
    from packages.common.auth_policy import trusted_internal_policy_decision

    root, repository, service = _seeded_journal(tmp_path)
    _refuse_scoped_job_records(monkeypatch, repository)
    context = pipeline_routes._RetryExecutionContext(
        policy_decision=trusted_internal_policy_decision(
            "pipeline.retry_run",
            target_type="pipeline_run",
            target_id=_RUN_ID,
            actor_id="trusted-internal:test",
            roles=("sys_admin",),
        ),
        service=service,
        gateway=_UnreachableGateway(),  # type: ignore[arg-type]
    )
    app.dependency_overrides[pipeline_routes.get_retry_execution_context] = lambda: context
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    try:
        response = TestClient(app).post(f"/api/v1/runs/{_RUN_ID}/retry", headers={"X-User-Role": "operator"})
    finally:
        app.dependency_overrides.pop(pipeline_routes.get_retry_execution_context, None)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RETRY_EVIDENCE_INVALID"
    assert error["details"]["run_id"] == _RUN_ID
    assert error["details"]["journal_reason"] == _BLOCK_REASON
    assert error["details"]["journal_field"] == _BLOCK_FIELD
    rendered = json.dumps(response.json())
    assert "Traceback" not in rendered
    assert str(root) not in rendered
    assert "full_tree_replay" not in rendered


def test_node22_operator_cli_reports_the_blocked_read_as_a_refused_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.7: the selector's third caller is outside ``main()``'s ``try``.

    Today the blocked row is filtered out and the CLI prints
    ``no_retryable_failed_job`` -- the command-line face of #2385.  After D2 the
    selector raises, and ``_preview`` is called at ``:100`` OUTSIDE the
    ``try`` at ``:102``, so without this task the operator gets a traceback and
    NO receipt at all.
    """

    journal = _journal()
    from scripts.node22_manual_retry_failed_runs import main

    root, repository, _service = _seeded_journal(tmp_path)
    del repository

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise _blocked_fault()

    # ``main`` builds its own repository from ``--journal-root``, so the seam is
    # the class attribute, not an instance.
    monkeypatch.setattr(journal.FileOrchestrationJournalRepository, "_iter_pipeline_job_records_scoped", _refuse)

    receipt_path = tmp_path / "receipt.json"
    exit_code = main(
        [
            "--journal-root",
            str(root),
            "--run-id",
            _RUN_ID,
            "--reason",
            "journal read refused",
            "--requested-by",
            "operator",
            "--output",
            str(receipt_path),
            "--execute",
        ]
    )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    entry = receipt["runs"][0]
    assert entry["preview"] == {
        "decision": "refused",
        "reason": "journal_read_blocked",
        "journal_reason": _BLOCK_REASON,
        "journal_field": _BLOCK_FIELD,
    }
    assert entry["outcome"] == "refused"
    # Unchanged exit code: a preview-time refusal under ``--execute`` is still 1.
    assert exit_code == 1
    assert [
        record
        for record in _journal_records(root, "pipeline_event")
        if (record.get("details") or {}).get("manual_retry_marker") is True
    ] == []


# --- 3. #2387 -- the chain retry classifier and the provenance readers ------


def _cycle_harness(tmp_path: Path, repository: Any, retry_service: Any) -> Any:
    """A real ``ForecastOrchestrator`` so ``handle_failed_job`` is actually reachable.

    ``_retry_job_for_stage_result`` alone cannot prove "the failed-job handler is
    never called": only its caller ``_schedule_cycle_stage_retry``
    (``chain_forecast_orchestrator_cycle.py:260``) reaches the handler.
    """

    from packages.common.object_store import LocalObjectStore
    from services.orchestrator.chain import ForecastOrchestrator, OrchestratorConfig

    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5.0,
    )
    return ForecastOrchestrator(
        config=config,
        repository=repository,
        slurm_client=object(),
        object_store=LocalObjectStore(object_root, "s3://nhms"),
        retry_service=retry_service,
    )


def _stage_result(job_id: str = _JOB_ID) -> Any:
    from services.orchestrator.chain import StageRunResult

    return StageRunResult(
        stage="forecast",
        job_type="run_shud_forecast_array",
        pipeline_job_id=job_id,
        slurm_job_id="551234",
        status="failed",
        error_code="SLURM_TIMEOUT",
        error_message="Slurm job hit its wall clock limit",
    )


def _spy_handle_failed_job(monkeypatch: pytest.MonkeyPatch, service: Any) -> list[Any]:
    handled: list[Any] = []
    real = service.handle_failed_job

    def _wrapped(job: Any) -> Any:
        handled.append(job)
        return real(job)

    monkeypatch.setattr(service, "handle_failed_job", _wrapped)
    return handled


def test_chain_classifier_refuses_a_blocked_by_id_read_with_a_classified_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.1: a refused read is not "this job is non-transient"."""

    from services.orchestrator.chain import OrchestratorError

    root, repository, service = _seeded_journal(tmp_path)
    orchestrator = _cycle_harness(tmp_path, repository, service)
    handled = _spy_handle_failed_job(monkeypatch, service)
    _refuse_job_id_reads(monkeypatch, repository)
    before = _tree_bytes(root)

    with pytest.raises(OrchestratorError) as error:
        orchestrator._schedule_cycle_stage_retry(_stage_result(), 1)

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    # The journal's OWN refusal reason, not a derived identity fault of the
    # classifier's own making.
    assert error.value.details["journal_reason"] == _BLOCK_REASON
    assert error.value.details["journal_field"] == _BLOCK_FIELD
    assert _BLOCK_REASON in error.value.message
    assert handled == []
    assert _tree_bytes(root) == before

    monkeypatch.undo()
    durable = repository.get_pipeline_job(_JOB_ID)
    assert durable is not None
    assert durable["status"] == "failed"
    assert durable["error_code"] == "SLURM_TIMEOUT"
    assert repository.get_pipeline_job(f"{_JOB_ID}_retry_1") is None


def test_chain_classifier_leaves_the_store_backed_database_lane_untouched(tmp_path: Path) -> None:
    """Task 3.2: the guard sits AFTER the ``store.get_job`` short-circuit.

    The store's answer is the REAL sentinel row -- minted by the journal's own
    ``_blocked_query_job``, marker and all -- precisely so this pin can fail for
    the reason it names: hoist the discriminator above the short-circuit and the
    classifier raises ``FILE_JOURNAL_READ_BLOCKED`` here instead of returning the
    database lane's answer.  A marker-free stand-in would be green on both sides,
    because ``_is_blocked_query_job`` answers False for anything unmarked.

    The store lane owns its own answer whatever it looks like: this row can only
    reach the classifier from a DATABASE-backed store, where the journal's
    refusal vocabulary means nothing, so the guard must not read it.
    """

    from services.orchestrator import chain_forecast_execution

    journal = _journal()
    _root, repository, service = _seeded_journal(tmp_path)
    store_job = journal._blocked_query_job(_blocked_fault(), job_id=_JOB_ID)
    assert store_job["file_journal"]["status"] == "blocked"
    assert store_job["file_journal"]["reason"] == _BLOCK_REASON
    assert store_job["file_journal"]["field"] == _BLOCK_FIELD
    # The precondition that makes the hoist detectable: the discriminator the
    # guard keys on says YES to this row.
    assert journal._is_blocked_query_job(store_job)

    class _Store:
        def __init__(self) -> None:
            self.session = None
            self.requested: list[str] = []

        def get_job(self, job_id: str) -> Any:
            self.requested.append(job_id)
            return store_job

    store = _Store()
    service.store = store  # type: ignore[attr-defined]
    orchestrator = _cycle_harness(tmp_path, repository, service)

    assert chain_forecast_execution._retry_job_for_stage_result(orchestrator, _stage_result()) is store_job
    assert store.requested == [_JOB_ID]


def test_the_guard_not_the_identity_sanitizer_carries_the_no_write_guarantee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.3 counterfactual: the by-run/by-cycle row shape carries real identity.

    The by-id lane's safety today is an accident: ``run_id``/``cycle_id`` are
    empty on that row, so ``_file_retry_job_record``'s identity sanitizer raises
    before ``mark_permanently_failed`` can write.  The by-run and by-cycle lanes
    mint the SAME marker with a real ``run_id`` and ``cycle_id``, and on that
    shape the sanitizer is silent -- pinned below.  With the guard removed the
    row reaches ``handle_failed_job``, is classified non-transient on the journal
    reason, and only a second accident (the row's DEFAULT ``job_id`` missing from
    the journal) stands between it and a ``permanently_failed`` write.  The guard
    must be what refuses, before any of that.
    """

    journal = _journal()
    from services.orchestrator.chain import OrchestratorError

    root, repository, service = _seeded_journal(tmp_path)
    blocked_row = journal._blocked_query_job(_blocked_fault(), run_id=_RUN_ID, cycle_id=_CYCLE_ID)
    assert blocked_row["run_id"] == _RUN_ID and blocked_row["cycle_id"] == _CYCLE_ID
    # The precondition that makes this the counterfactual: the sanitizer that
    # carries the by-id lane's safety does not raise on the job the classifier
    # would build from this row.
    from services.orchestrator.chain import PipelineJob

    would_be_job = PipelineJob(
        job_id=str(blocked_row["job_id"]),
        run_id=blocked_row["run_id"],
        cycle_id=blocked_row["cycle_id"],
        job_type="run_shud_forecast_array",
        slurm_job_id=blocked_row["slurm_job_id"],
        model_id=blocked_row.get("model_id"),
        status=str(blocked_row["status"]),
        stage=blocked_row["stage"],
    )
    sanitized = journal._file_retry_job_record(would_be_job)
    assert sanitized["run_id"] == _RUN_ID
    assert sanitized["cycle_id"] == _CYCLE_ID

    orchestrator = _cycle_harness(tmp_path, repository, service)
    handled = _spy_handle_failed_job(monkeypatch, service)
    monkeypatch.setattr(repository, "get_pipeline_job", lambda _job_id: dict(blocked_row))
    before = _tree_bytes(root)

    with pytest.raises(OrchestratorError) as error:
        orchestrator._schedule_cycle_stage_retry(_stage_result(), 1)

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert error.value.details["journal_reason"] == _BLOCK_REASON
    assert handled == []
    assert _tree_bytes(root) == before

    monkeypatch.undo()
    durable = repository.get_pipeline_job(_JOB_ID)
    assert durable is not None
    assert durable["status"] == "failed"
    assert durable["status"] != "permanently_failed"
    assert repository.get_pipeline_job(f"{_JOB_ID}_retry_1") is None


def test_provenance_readers_degrade_with_a_warning_and_the_sibling_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Task 3.4: empty batch / ``None`` plus the journal reason and field in the log."""

    _root, repository, service = _seeded_journal(tmp_path)
    _refuse_job_id_reads(monkeypatch, repository)

    with caplog.at_level(logging.WARNING, logger=_JOURNAL_LOGGER):
        assert service._file_retry_previous_job_id(_JOB_ID) is None
        batch = service._file_retry_event_runtime_root_candidates(_JOB_ID, candidate_budget=4)
        # The correct precedent for the degrade shape, untouched by this change:
        # it already recognises the marker and returns ``None`` without a warning
        # of its own on that branch.
        assert service.submission_runtime_root_resolution(_JOB_ID) is None

    assert batch.candidates == []
    assert batch.event_candidate_returned_count == 0
    messages = [record.getMessage() for record in caplog.records if record.name == _JOURNAL_LOGGER]
    assert len(messages) == 2
    for message in messages:
        assert _BLOCK_REASON in message
        assert _BLOCK_FIELD in message


def _arm_predecessor_refusal_after_mint(
    monkeypatch: pytest.MonkeyPatch, repository: Any, service: Any
) -> dict[str, bool]:
    """Refuse the PREDECESSOR's by-id read only once the pending retry row exists.

    The refusal must land after ``_create_pending_manual_retry_job`` (which reads
    the same id through the same private method) and must not touch the retry
    row's own id, which ``update_pipeline_job_status`` reads on both outcomes.
    """

    state = {"armed": False}
    real_read = repository._pipeline_job_for_id_unlocked

    def _read(job_id: str, *args: Any, **kwargs: Any) -> Any:
        if state["armed"] and str(job_id) == _JOB_ID:
            raise _blocked_fault()
        return real_read(job_id, *args, **kwargs)

    monkeypatch.setattr(repository, "_pipeline_job_for_id_unlocked", _read)

    real_mint = service._create_pending_manual_retry_job

    def _mint(run_id: str) -> Any:
        minted = real_mint(run_id)
        state["armed"] = True
        return minted

    monkeypatch.setattr(service, "_create_pending_manual_retry_job", _mint)
    return state


def test_blocked_predecessor_read_no_longer_poisons_the_recorded_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Task 3.5, against the CORRECTED premise.

    The issue's "unclassified 500" is stale: the provenance walk runs inside
    ``attempt_manual_retry``'s ``except Exception``, AFTER the pending retry row
    is minted.  So today the refusal is caught, ``_retry_submission_error_code``
    finds no ``.code`` on ``FileOrchestrationJournalError`` and falls back to
    ``SBATCH_SUBMISSION_FAILED``, and the fresh retry row is persisted as
    ``submission_failed`` with a fabricated gateway code and a
    ``file_journal_missing_identity``-derived message -- blaming a gateway that
    was never called.  After the fix the walk skips the unreadable candidate with
    a warning and resolves the roots from the environment instead.
    """

    from fastapi.testclient import TestClient

    from apps.api.main import app
    from apps.api.routes import pipeline as pipeline_routes
    from packages.common.auth_policy import trusted_internal_policy_decision

    root, repository, service = _seeded_journal(tmp_path)
    workspace_root = (tmp_path / "roots" / "workspace").resolve()
    object_store_root = (tmp_path / "roots" / "object-store").resolve()
    workspace_root.mkdir(parents=True)
    object_store_root.mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store_root))

    gateway = _SuccessGateway()
    _arm_predecessor_refusal_after_mint(monkeypatch, repository, service)
    context = pipeline_routes._RetryExecutionContext(
        policy_decision=trusted_internal_policy_decision(
            "pipeline.retry_run",
            target_type="pipeline_run",
            target_id=_RUN_ID,
            actor_id="trusted-internal:test",
            roles=("sys_admin",),
        ),
        service=service,
        gateway=gateway,  # type: ignore[arg-type]
    )
    app.dependency_overrides[pipeline_routes.get_retry_execution_context] = lambda: context
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    try:
        with caplog.at_level(logging.WARNING, logger=_JOURNAL_LOGGER):
            response = TestClient(app).post(f"/api/v1/runs/{_RUN_ID}/retry", headers={"X-User-Role": "operator"})
    finally:
        app.dependency_overrides.pop(pipeline_routes.get_retry_execution_context, None)

    assert response.status_code == 200, response.json()
    body = response.json()["data"]
    assert body["status"] == "submitted"
    assert gateway.requests, "the walk must degrade and let the submission proceed"

    # The durable retry row itself, not just the wire: no fabricated gateway
    # code, no identity-fault message recorded against it.
    retry_row = repository.get_pipeline_job(str(body["job_id"]))
    assert retry_row is not None
    assert retry_row["status"] == "submitted"
    assert retry_row["error_code"] is None
    assert retry_row["error_message"] is None

    submission_events = [
        record
        for record in _journal_records(root, "pipeline_event")
        if str(record.get("event_type") or "") == "submission"
    ]
    assert submission_events
    for event in submission_events:
        assert str(event.get("status_to") or "") != "submission_failed"
        assert "SBATCH_SUBMISSION_FAILED" not in json.dumps(event)
        assert "file_journal_missing_identity" not in json.dumps(event)

    messages = [record.getMessage() for record in caplog.records if record.name == _JOURNAL_LOGGER]
    assert any(_BLOCK_REASON in message and _BLOCK_FIELD in message for message in messages)


# --- 3.6 sibling surfaces that must keep their current verdicts -------------


def test_blocked_row_keeps_its_load_bearing_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 3.6: the fail-closed guards named in ``_blocked_query_job``'s docstring."""

    journal = _journal()
    _root, repository, _service = _seeded_journal(tmp_path)
    blocked_row = journal._blocked_query_job(_blocked_fault(), job_id=_JOB_ID)

    assert journal._job_is_active(blocked_row) is True
    assert journal._file_auto_retry_job_can_be_reused(blocked_row) is False

    _refuse_job_id_reads(monkeypatch, repository)
    assert repository._pipeline_job_conflicts_unlocked({"job_id": _JOB_ID}) is True


def test_chain_array_accounting_terminal_allowlist_still_excludes_the_blocked_status() -> None:
    """Task 3.6: not an instance of this bug -- the allowlist already fails closed."""

    journal = _journal()
    from services.orchestrator import chain_array_accounting

    source = inspect.getsource(chain_array_accounting.record_cycle_stage_status_override)
    allowlists = [
        {element.value for element in node.elts if isinstance(element, ast.Constant)}
        for node in ast.walk(ast.parse(inspect.cleandoc(source)))
        if isinstance(node, ast.Set)
    ]
    terminal_allowlists = [names for names in allowlists if "reconcile_unverified" in names]
    assert terminal_allowlists, "the terminal-status allowlist moved; re-point this pin"
    for names in terminal_allowlists:
        assert journal.FILE_JOURNAL_READ_BLOCKED_STATUS not in names
