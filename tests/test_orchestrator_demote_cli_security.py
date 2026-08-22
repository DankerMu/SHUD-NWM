"""#1564 demote-reserved-job: CLI entrypoints and no-secret stdout/durable bytes.

Both entrypoints (Click and argparse) enforce ``--confirm`` before the
repository is constructed, print stable sorted JSON, exit 0 on success and 2
on any validation/CAS/confirmation failure, and sanitize secret-shaped and
path-shaped operator evidence identically in the receipt and every durable
byte.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator.accepted_submit_identity import OPERATOR_VERIFIED_ABSENCE_DECISION
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from tests.orchestrator_demote_reserved_job_helpers import (
    JOB_ID,
    PATH_SECRET_CHECKED_BY,
    PATH_SECRET_LITERALS,
    PATH_SECRET_VERIFICATION_NOTE,
    SECRET_LITERALS,
    STARTED_AT,
    _cli_args_with_secrets,
    _cli_base_args,
    _demote_kwargs,
    _durable_bytes,
    _durable_event_payloads,
    _held_cohort_repository,
    _held_row,
    _journal_bytes,
)


# ---------------------------------------------------------------------------
# 3.1 / 3.2 CLI entrypoints
# ---------------------------------------------------------------------------
def test_cli_demote_missing_confirm_fails_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    held = _held_row(held_repository)
    before = _journal_bytes(held_repository.root)

    class _RefusingRepository:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("repository must not be constructed when --confirm is absent")

    monkeypatch.setattr(
        "services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository",
        _RefusingRepository,
    )

    args = _cli_base_args(held_repository, held)
    # click with standalone_mode=False raises MissingParameter (exit code 2 in
    # standalone mode) before the command body runs.
    import click

    with pytest.raises(click.MissingParameter):
        cli._click_main(args)
    rc = cli._argparse_main(args)
    assert rc == 2
    assert _journal_bytes(held_repository.root) == before


def test_cli_demote_missing_confirm_argparse_exits_2_with_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    held = _held_row(held_repository)
    before = _journal_bytes(held_repository.root)

    class _RefusingRepository:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("repository must not be constructed when --confirm is absent")

    monkeypatch.setattr(
        "services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository",
        _RefusingRepository,
    )

    rc = cli._argparse_main(_cli_base_args(held_repository, held))
    assert rc == 2
    captured = capsys.readouterr()
    assert "--confirm" in captured.err
    assert _journal_bytes(held_repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_success_prints_stable_sorted_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    before = _journal_bytes(root)
    args = _cli_base_args(held_repository, held)
    if entrypoint == "click":
        # click's required --confirm is satisfied only when the flag is present.
        args = [*args, "--confirm"]
        rc = cli._click_main(args)
    else:
        args = [*args, "--confirm"]
        rc = cli._argparse_main(args)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["command"] == "demote-reserved-job"
    assert payload["status"] == "demoted"
    assert payload["job_id"] == JOB_ID
    assert payload["status_from"] == "reserved"
    assert payload["status_to"] == "reservation_lost"
    assert payload["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert payload["submission_attempt"] == 1
    assert payload["written_record_count"] > 0
    assert payload["checked_by"] == "operator-alice"
    assert payload["journal_root"] == str(root.resolve())
    # stdout is exactly one sorted JSON object.
    assert json.dumps(payload, sort_keys=True) == out
    # The journal advanced past the held state.
    assert _journal_bytes(root) != before
    current = FileOrchestrationJournalRepository(root).get_accepted_submit_pipeline_job(JOB_ID)
    assert current["status"] == "reservation_lost"
    assert current["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_cas_refusal_exits_2_with_stderr_and_zero_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    before = _journal_bytes(root)
    args = [
        "demote-reserved-job",
        "--journal-root",
        str(root),
        "--job-id",
        JOB_ID,
        "--expected-attempt",
        str(int(held["submission_attempt"]) + 7),
        "--expected-attempt-started-at",
        str(held["submission_attempt_started_at"]),
        "--checked-by",
        "operator-alice",
        "--checked-at",
        (STARTED_AT + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "--verification-note",
        "stale attempt expectation",
    ]
    if entrypoint == "click":
        with pytest.raises(SystemExit) as excinfo:
            cli._click_main([*args, "--confirm"])
        assert excinfo.value.code == 2
    else:
        assert cli._argparse_main([*args, "--confirm"]) == 2
    captured = capsys.readouterr()
    assert "compare-and-swap refused" in captured.err
    assert captured.out.strip() == ""
    assert _journal_bytes(root) == before
    assert _held_row(held_repository) == held


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_validation_error_exits_2_with_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    before = _journal_bytes(root)
    args = [
        "demote-reserved-job",
        "--journal-root",
        str(root),
        "--job-id",
        JOB_ID,
        "--expected-attempt",
        "1",
        "--expected-attempt-started-at",
        "2026-07-12T00:00:00",  # missing timezone
        "--checked-by",
        "operator-alice",
        "--checked-at",
        (STARTED_AT + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "--verification-note",
        "bad anchor timezone",
    ]
    if entrypoint == "click":
        with pytest.raises(SystemExit) as excinfo:
            cli._click_main([*args, "--confirm"])
        assert excinfo.value.code == 2
    else:
        assert cli._argparse_main([*args, "--confirm"]) == 2
    captured = capsys.readouterr()
    assert "timezone" in captured.err
    assert _journal_bytes(root) == before
    assert _held_row(held_repository) == held


def test_cli_both_entrypoints_emit_identical_sorted_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    click_repo = _held_cohort_repository(tmp_path / "click-held")
    argparse_repo = _held_cohort_repository(tmp_path / "argparse-held")
    click_row = _held_row(click_repo)
    argparse_row = _held_row(argparse_repo)

    def _args(repository: Any, row: dict[str, Any]) -> list[str]:
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
            "--confirm",
        ]

    assert cli._click_main(_args(click_repo, click_row)) == 0
    click_payload = json.loads(capsys.readouterr().out.strip())
    assert cli._argparse_main(_args(argparse_repo, argparse_row)) == 0
    argparse_payload = json.loads(capsys.readouterr().out.strip())

    # The only differing field is the journal root each entrypoint was pointed
    # at; every contract field must be byte-identical.
    assert click_payload["journal_root"] != argparse_payload["journal_root"]
    click_without_root = {k: v for k, v in click_payload.items() if k != "journal_root"}
    argparse_without_root = {k: v for k, v in argparse_payload.items() if k != "journal_root"}
    assert click_without_root == argparse_without_root
    assert click_payload["status"] == "demoted"
    assert click_payload["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_post_commit_projection_faults_report_committed_with_warnings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    from services.orchestrator.chain_types import OrchestratorError

    held_repository = _held_cohort_repository(tmp_path / "held", member_count=2, active_hydro=True)
    root = held_repository.root
    held = _held_row(held_repository)
    args = [*_cli_base_args(held_repository, held), "--confirm"]

    def fail_direct(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError(
            "FILE_JOURNAL_WRITE_FAILED",
            "injected direct projection failure with https://minio.example/bucket/obj?X-Amz-Credential=AKIAEXAMPLE",
        )

    def fail_latest(*, source_id: Any, cycle_time: Any, model_id: str, **kwargs: Any) -> None:
        raise FileOrchestrationJournalError(
            "file_journal_byte_limit_exceeded",
            field=str(Path(f"/tmp/secret-{model_id}/latest.json").resolve()),
        )

    monkeypatch.setattr(
        held_repository,
        "_write_pipeline_job_direct_unlocked",
        fail_direct,
    )
    monkeypatch.setattr(held_repository, "_materialize_latest_unlocked", fail_latest)
    monkeypatch.setattr(
        "services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository",
        lambda *a, **k: held_repository,
    )

    if entrypoint == "click":
        assert cli._click_main(args) == 0
    else:
        assert cli._argparse_main(args) == 0
    out = capsys.readouterr().out.strip()
    assert capsys.readouterr().err == ""
    payload = json.loads(out)
    assert payload["status"] == "demoted_with_warnings"
    assert payload["committed"] is True
    # Both projections failed and all warnings carry the fixed non-secret token
    # (never the class label or code, even for exact trusted typed exceptions).
    assert payload["warnings"] == [
        {
            "projection": "latest",
            "model_id": "model_0",
            "error_type": "projection_fault",
            "reason": "projection_fault",
        },
        {
            "projection": "latest",
            "model_id": "model_1",
            "error_type": "projection_fault",
            "reason": "projection_fault",
        },
        {
            "projection": "pipeline_job_direct",
            "model_id": None,
            "error_type": "projection_fault",
            "reason": "projection_fault",
        },
    ]
    # Warnings are bounded and never carry exception text, paths, or secrets.
    # The scan targets the warning-only structure: the legal public receipt
    # field ``journal_root`` legitimately points under the platform temp dir on
    # CI, so a whole-payload scan would false-red on ``/tmp/`` while the
    # warnings themselves must never carry the injected fault evidence.
    rendered = json.dumps(payload["warnings"], sort_keys=True)
    assert "AKIAEXAMPLE" not in rendered
    assert "/tmp/" not in rendered
    assert "secret-" not in rendered
    assert "injected direct projection failure" not in rendered
    # The legal public receipt field is pinned to the real resolved journal root.
    assert payload["journal_root"] == str(root.resolve())
    assert payload["status_from"] == "reserved"
    assert payload["status_to"] == "reservation_lost"
    assert payload["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    # The authority batch is durable despite the projection faults.
    assert _held_row(held_repository)["status"] == "reservation_lost"
    assert len(_durable_event_payloads(root)) == 1


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_success_does_not_resolve_path_after_commit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """The CLI never runs fallible path resolution after the authority commit.

    ``Path(...).resolve()`` can raise (symlink loop / OSError); doing it after a
    committed demotion would turn a durable success into a reported failure.  A
    receipt whose ``journal_root`` is an unresolvable symlink loop must still
    produce a successful receipt with the precomputed real display root.
    """

    held_repository = _held_cohort_repository(tmp_path / "held", member_count=2, active_hydro=True)
    root = held_repository.root
    held = _held_row(held_repository)
    args = [*_cli_base_args(held_repository, held), "--confirm"]

    # A symlink loop that would make Path.resolve() raise OSError.
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    hostile_root = str(loop_a)

    real_receipt = held_repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(held),
    )
    assert real_receipt is not None

    from services.orchestrator.file_orchestration_journal import OperatorDemoteReceipt

    hostile_receipt = OperatorDemoteReceipt(
        **{
            **real_receipt.__dict__,
            "journal_root": hostile_root,
        }
    )

    def committed(*_args: Any, **_kwargs: Any) -> OperatorDemoteReceipt:
        return hostile_receipt

    monkeypatch.setattr(held_repository, "demote_operator_verified_reserved_job", committed)
    monkeypatch.setattr(
        "services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository",
        lambda *a, **k: held_repository,
    )

    if entrypoint == "click":
        rc = cli._click_main(args)
    else:
        rc = cli._argparse_main(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["committed"] is True
    assert payload["status"] == "demoted"
    # The display root is the precomputed resolved REAL root, not the hostile
    # loop path, and no post-commit resolve() ran.
    assert payload["journal_root"] == str(root.resolve())
    assert payload["journal_root"] != hostile_root


# ---------------------------------------------------------------------------
# 3.4 no-secret CLI stdout and durable files (secret-shaped operator evidence)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_success_stdout_and_durable_files_never_leak_secret_literals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    args = _cli_args_with_secrets(held_repository, held)
    if entrypoint == "click":
        rc = cli._click_main([*args, "--confirm"])
    else:
        rc = cli._argparse_main([*args, "--confirm"])
    assert rc == 0
    stdout = capsys.readouterr().out.strip()
    payload = json.loads(stdout)
    # The receipt echoes exactly the normalized/redacted operator strings.
    for literal in SECRET_LITERALS:
        assert literal not in stdout
        assert literal not in json.dumps(payload)
    assert "[redacted]" in payload["checked_by"]
    assert "[redacted]" in payload["verification_note"]
    # The durable journal/event/direct/latest bytes carry the same guarantee.
    durable = _durable_bytes(root)
    for literal in SECRET_LITERALS:
        assert literal.encode() not in durable
    assert b"[redacted]" in durable
    # The audit event itself stores the redacted forms.
    events = _durable_event_payloads(root)
    assert len(events) == 1
    assert "[redacted]" in events[0]["details"]["checked_by"]
    assert "[redacted]" in events[0]["details"]["verification_note"]


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_demote_local_paths_and_object_uris_sanitized_in_stdout_and_durable_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """Local paths / object URIs / credentials never reach stdout or durable bytes.

    The demotion inline batch bypasses ``_public_pipeline_event_payload``, so
    the operator-evidence authority itself must sanitize: local paths become
    ``[local-path]``, object URIs ``[uri]``, credentials ``[redacted]`` -- in
    both the CLI receipt and every durable byte, with the receipt fields equal
    to the audit-event details byte-for-byte.
    """

    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    args = _cli_base_args(held_repository, held)
    args[args.index("--checked-by") + 1] = PATH_SECRET_CHECKED_BY
    args[args.index("--verification-note") + 1] = PATH_SECRET_VERIFICATION_NOTE

    if entrypoint == "click":
        rc = cli._click_main([*args, "--confirm"])
    else:
        rc = cli._argparse_main([*args, "--confirm"])
    assert rc == 0
    stdout = capsys.readouterr().out.strip()
    payload = json.loads(stdout)
    for literal in PATH_SECRET_LITERALS:
        assert literal not in stdout
        assert literal not in json.dumps(payload)
    # Sanitized placeholders appear as applicable.
    assert "[local-path]" in payload["checked_by"]
    assert "[local-path]" in payload["verification_note"]
    assert "[uri]" in payload["verification_note"]
    assert "[redacted]" in payload["verification_note"]
    # The durable journal/event/direct/latest bytes carry the same guarantee.
    durable = _durable_bytes(root)
    for literal in PATH_SECRET_LITERALS:
        assert literal.encode() not in durable
    assert b"[local-path]" in durable
    assert b"[uri]" in durable
    assert b"[redacted]" in durable
    # The audit event stores the same sanitized forms and agrees byte-for-byte
    # with the receipt.
    events = _durable_event_payloads(root)
    assert len(events) == 1
    details = events[0]["details"]
    assert payload["checked_by"] == details["checked_by"]
    assert payload["verification_note"] == details["verification_note"]


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cli_secret_redaction_keeps_same_normalized_values_in_durable_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    held_repository = _held_cohort_repository(tmp_path / "held")
    root = held_repository.root
    held = _held_row(held_repository)
    args = _cli_args_with_secrets(held_repository, held)
    if entrypoint == "click":
        rc = cli._click_main([*args, "--confirm"])
    else:
        rc = cli._argparse_main([*args, "--confirm"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    events = _durable_event_payloads(root)
    assert len(events) == 1
    event = events[0]["details"]
    # stdout and the durable event agree byte-for-byte on every operator string.
    assert payload["checked_by"] == event["checked_by"]
    assert payload["checked_at"] == event["checked_at"]
    assert payload["verification_note"] == event["verification_note"]


def test_cli_both_entrypoints_emit_same_normalized_redacted_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    click_repo = _held_cohort_repository(tmp_path / "click-held")
    argparse_repo = _held_cohort_repository(tmp_path / "argparse-held")

    assert cli._click_main([*_cli_args_with_secrets(click_repo, _held_row(click_repo)), "--confirm"]) == 0
    click_payload = json.loads(capsys.readouterr().out.strip())
    assert cli._argparse_main([*_cli_args_with_secrets(argparse_repo, _held_row(argparse_repo)), "--confirm"]) == 0
    argparse_payload = json.loads(capsys.readouterr().out.strip())

    assert click_payload["journal_root"] != argparse_payload["journal_root"]
    click_without_root = {k: v for k, v in click_payload.items() if k != "journal_root"}
    argparse_without_root = {k: v for k, v in argparse_payload.items() if k != "journal_root"}
    assert click_without_root == argparse_without_root
    assert "[redacted]" in click_payload["checked_by"]
    assert "[redacted]" in argparse_payload["verification_note"]

def test_cli_demote_secret_shaped_projection_fault_warns_with_bounded_non_secret_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1564 regression: EVERY caught projection exception maps to one fixed token.

    A projection exception may carry an oversized/secret-shaped class name and
    ``.reason``/``.error_code`` -- including compact secret-shaped values that
    are themselves syntactically safe tokens, and including EXACT base typed
    exceptions whose constructors accept arbitrary code/reason strings.  The
    success receipt must still exit 0, report the authority batch as committed
    and durable, and carry warnings whose ``error_type`` and ``reason`` are
    the SAME fixed constant for every input, so the public output carries zero
    secret-derived identity and stays non-secret by construction.
    """

    from services.orchestrator import cli
    from services.orchestrator.chain_types import OrchestratorError
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError
    from tests.orchestrator_demote_reserved_job_helpers import _cli_base_args

    class SecretTokenClass(RuntimeError):
        """An arbitrary exception whose class name is a safe identifier."""

    class TrustedSubclass(OrchestratorError):
        """A subclass of a trusted typed exception -- still NOT trusted."""

    def _untrusted_with(attr: str, value: str) -> Exception:
        error = SecretTokenClass(value)
        setattr(error, attr, value)
        return error

    def _dynamic_secret_class_error(reason: str, class_name: str) -> Exception:
        error = type(class_name, (RuntimeError,), {})(reason)
        error.reason = reason
        return error

    # (make_error, literals_never_present): each distinct secret-shaped input
    # must yield the SAME fixed constant.
    scenarios = [
        (
            lambda: _dynamic_secret_class_error(
                "password=supersecret /private/secret-path/projections/token=tok_live_123 " + "x" * 300,
                "password=supersecret /private/secret-var/ProjectionError" + "y" * 300,
            ),
            (
                "supersecret",
                "secret-path",
                "secret-var",
                "tok_live_123",
                "password=",
                "ProjectionError",
                "x" * 16,
                "y" * 16,
            ),
        ),
        (
            lambda: _untrusted_with(
                "reason",
                "Authorization: Bearer abc.def.ghi Basic dXNlcjpwYXNz token=tok_live_999 " + "z" * 300,
            ),
            ("abc.def.ghi", "dXNlcjpwYXNz", "tok_live_999", "SecretTokenClass", "z" * 16),
        ),
        (
            # Compact secret-shaped reason that is itself a valid safe token.
            lambda: _untrusted_with("reason", "supersecret"),
            ("supersecret", "SecretTokenClass"),
        ),
        (
            # Compact secret-shaped error_code on an arbitrary exception.
            lambda: _untrusted_with("error_code", "tok_live_123"),
            ("tok_live_123", "SecretTokenClass"),
        ),
        (
            # A subclass of a trusted typed exception is still not trusted.
            lambda: TrustedSubclass("tok_live_123", "boom"),
            ("tok_live_123", "TrustedSubclass"),
        ),
        (
            # EXACT base typed exception with a compact secret error_code.
            lambda: OrchestratorError("supersecret", "boom"),
            ("supersecret", "OrchestratorError"),
        ),
        (
            # EXACT base typed exception with a compact secret reason.
            lambda: FileOrchestrationJournalError("tok_live_123", field="latest"),
            ("tok_live_123", "FileOrchestrationJournalError"),
        ),
    ]
    seen_error_types: list[str] = []
    seen_reasons: list[str] = []
    for index, (make_error, literals) in enumerate(scenarios):
        held_repository = _held_cohort_repository(
            tmp_path / f"held-{index}", member_count=2, active_hydro=True
        )
        root = held_repository.root
        held = _held_row(held_repository)
        args = [*_cli_base_args(held_repository, held), "--confirm"]

        def fail_direct(*_args: Any, **_kwargs: Any) -> None:
            raise make_error()

        def fail_latest(*, source_id: Any, cycle_time: Any, model_id: str, **kwargs: Any) -> None:
            del source_id, cycle_time, kwargs
            raise make_error()

        monkeypatch.setattr(held_repository, "_write_pipeline_job_direct_unlocked", fail_direct)
        monkeypatch.setattr(held_repository, "_materialize_latest_unlocked", fail_latest)
        monkeypatch.setattr(
            "services.orchestrator.operator_reserved_demotion.FileOrchestrationJournalRepository",
            lambda *a, **k: held_repository,
        )

        assert cli._argparse_main(args) == 0
        out = capsys.readouterr().out.strip()
        assert capsys.readouterr().err == ""
        payload = json.loads(out)
        assert payload["status"] == "demoted_with_warnings"
        assert payload["committed"] is True
        assert payload["status_from"] == "reserved"
        assert payload["status_to"] == "reservation_lost"
        assert payload["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
        assert len(payload["warnings"]) == 3
        for warning in payload["warnings"]:
            assert warning["error_type"] == "projection_fault", warning
            assert warning["reason"] == "projection_fault", warning
            seen_error_types.append(warning["error_type"])
            seen_reasons.append(warning["reason"])
        rendered = json.dumps(payload, sort_keys=True)
        for literal in literals:
            assert literal not in rendered, literal
        # The authority batch is durable despite the secret-shaped projection faults.
        current = FileOrchestrationJournalRepository(root).get_accepted_submit_pipeline_job(JOB_ID)
        assert current["status"] == "reservation_lost"
        assert current["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
        events = _durable_event_payloads(root)
        assert len(events) == 1
        assert events[0]["event_type"] == "operator_verified_absence"
        # Repeating the request after the warning is still a zero-write CAS refusal.
        before_repeat = _journal_bytes(root)
        assert cli._argparse_main(args) == 2
        assert _journal_bytes(root) == before_repeat
        assert len(_durable_event_payloads(root)) == 1
    # Every warning across every distinct input is the SAME fixed constant: no
    # secret-derived identity ever reaches the public receipt, whether the
    # input is prose, a path, a compact secret, a safe identifier class name, a
    # trusted-type subclass, or an EXACT base typed exception.
    expected_count = 3 * len(scenarios)
    assert seen_error_types == ["projection_fault"] * expected_count
    assert seen_reasons == ["projection_fault"] * expected_count
