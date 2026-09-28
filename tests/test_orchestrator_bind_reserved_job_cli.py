"""#2668 bind-reserved-job: both CLI entrypoints, confirmation, redaction, projection faults.

Drives the shipped ``cli._click_main`` / ``cli._argparse_main`` entrypoints.
``--confirm``, blank evidence and naive timestamps fail before the repository
is constructed; success prints one stable sorted-key JSON receipt carrying only
the normalized/redacted evidence the durable ``operator_verified_bind`` event
recorded; a post-commit projection fault is reported committed with warnings,
and a retried request is a zero-write ``not_held``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest

from services.orchestrator import cli
from services.orchestrator.chain_types import OrchestratorError
from tests.orchestrator_bind_reserved_job_helpers import (
    CHECKED_AT,
    JOB_ID,
    MASTER_ID,
    MASTER_SUBMIT,
    OWN_COMMENT,
    bind_events,
    cli_args,
    held_repository,
    held_row,
    journal_bytes,
)
from tests.orchestrator_demote_reserved_job_helpers import (
    SECRET_CHECKED_BY,
    SECRET_LITERALS,
    SECRET_VERIFICATION_NOTE,
)
from tests.test_real_slurm_gateway import _pinned_local_timezone

pytestmark = pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="time.tzset() is POSIX-only")

_REPOSITORY_SEAM = "services.orchestrator.operator_reserved_bind.FileOrchestrationJournalRepository"


@pytest.fixture(autouse=True)
def _utc_local_time() -> Iterator[None]:
    with _pinned_local_timezone("UTC"):
        yield


def _invoke(entrypoint: str, args: list[str]) -> int:
    if entrypoint == "click":
        try:
            return int(cli._click_main(args) or 0)
        except SystemExit as error:
            return int(error.code or 0)
    return cli._argparse_main(args)


class _RefusingRepository:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the repository must not be constructed")


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_success_prints_the_stable_sorted_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    held = held_row(repository)

    assert _invoke(entrypoint, cli_args(repository.root, held)) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = captured.out.strip()
    payload = json.loads(out)
    assert out == json.dumps(payload, sort_keys=True)
    assert payload == {
        "command": "bind-reserved-job",
        "status": "bound",
        "committed": True,
        "journal_root": str(repository.root.resolve()),
        "job_id": JOB_ID,
        "status_from": "reserved",
        "status_to": "submitted",
        "reconciliation_source": "slurm_name_window_unique",
        "reconciliation_decision": "matched_bound",
        "matched_slurm_job_id": MASTER_ID,
        "slurm_accounting_submitted_at": MASTER_SUBMIT,
        "submitline_key": OWN_COMMENT,
        "submission_attempt": 1,
        "submission_attempt_started_at": "2026-07-12T00:00:13Z",
        "checked_by": "operator-alice",
        "checked_at": CHECKED_AT,
        "verification_note": payload["verification_note"],
        "written_record_count": 2,
        "warnings": [],
    }
    assert len(bind_events(repository.root)) == 1
    # The raw SubmitLine (script paths) is never echoed.
    assert "nhms_6y2bx17b.sbatch" not in out


def test_both_entrypoints_emit_the_same_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payloads = []
    for entrypoint in ("click", "argparse"):
        repository = held_repository(tmp_path / entrypoint, monkeypatch)
        assert _invoke(entrypoint, cli_args(repository.root, held_row(repository))) == 0
        payload = json.loads(capsys.readouterr().out)
        payload.pop("journal_root")
        payloads.append(payload)
    assert payloads[0] == payloads[1]


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_missing_confirm_fails_before_the_repository_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    before = journal_bytes(repository.root)
    monkeypatch.setattr(_REPOSITORY_SEAM, _RefusingRepository)
    args = cli_args(repository.root, held_row(repository), confirm=False)

    if entrypoint == "click":
        # standalone_mode=False surfaces Click's own missing-option refusal.
        with pytest.raises(click.MissingParameter):
            cli._click_main(args)
    else:
        assert cli._argparse_main(args) == 2
        assert "--confirm" in capsys.readouterr().err
    assert journal_bytes(repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--checked-by", "   "),
        ("--verification-note", ""),
        ("--submit-line", "  "),
        ("--slurm-job-id", " "),
        ("--slurm-submit-time", "2026-07-12T00:02:23"),  # sacct's local naive Submit, no offset
        ("--checked-at", "2026-07-12T02:30:00"),
        ("--expected-attempt-started-at", "2026-07-12T00:00:13"),
        ("--checked-at", "not-a-time"),
        ("--expected-attempt", "0"),
    ],
)
def test_blank_evidence_or_naive_timestamps_fail_before_the_repository_is_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    option: str,
    value: str,
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    before = journal_bytes(repository.root)
    monkeypatch.setattr(_REPOSITORY_SEAM, _RefusingRepository)

    assert _invoke(entrypoint, cli_args(repository.root, held_row(repository), **{option: value})) == 2

    err = capsys.readouterr().err
    assert "bind-reserved-job" in err and option in err
    assert journal_bytes(repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
@pytest.mark.parametrize(
    ("option", "value", "refusal"),
    [
        (
            "--submit-line",
            "/usr/bin/sbatch --comment=nhms_idem:other:forecast /tmp/x.sbatch",
            "submitline_key_mismatch",
        ),
        ("--slurm-submit-time", "2026-07-11T23:00:00Z", "submit_time_outside_attempt_window"),
        ("--slurm-job-id", "56839_0", "slurm_id_invalid"),
        ("--expected-attempt", "2", "stale_attempt"),
    ],
)
def test_a_named_refusal_exits_2_on_stderr_with_zero_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    option: str,
    value: str,
    refusal: str,
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    before = journal_bytes(repository.root)

    assert _invoke(entrypoint, cli_args(repository.root, held_row(repository), **{option: value})) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"bind-reserved-job: refused: {refusal}; no journal bytes were written"
    assert journal_bytes(repository.root) == before


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_secret_shaped_evidence_is_redacted_in_the_receipt_and_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    args = cli_args(
        repository.root,
        held_row(repository),
        **{"--checked-by": SECRET_CHECKED_BY, "--verification-note": SECRET_VERIFICATION_NOTE},
    )

    assert _invoke(entrypoint, args) == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    durable = b"".join(journal_bytes(repository.root).values())
    for literal in SECRET_LITERALS:
        assert literal not in out
        assert literal.encode() not in durable
    (event,) = bind_events(repository.root)
    assert "[redacted]" in payload["checked_by"] and "[redacted]" in payload["verification_note"]
    # One authority: the receipt echoes exactly what the durable event stored.
    assert (event["details"]["checked_by"], event["details"]["verification_note"]) == (
        payload["checked_by"],
        payload["verification_note"],
    )


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_a_post_commit_projection_fault_is_committed_with_warnings_and_a_retry_is_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    repository = held_repository(tmp_path, monkeypatch)
    held = held_row(repository)

    def _fail_direct(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError(
            "FILE_JOURNAL_WRITE_FAILED",
            "injected with https://minio.example/bucket/obj?X-Amz-Credential=AKIAEXAMPLE",
        )

    monkeypatch.setattr(repository, "_write_pipeline_job_direct_unlocked", _fail_direct)
    monkeypatch.setattr(_REPOSITORY_SEAM, lambda *_a, **_k: repository)

    assert _invoke(entrypoint, cli_args(repository.root, held)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert (payload["status"], payload["committed"], payload["written_record_count"]) == (
        "bound_with_warnings",
        True,
        2,
    )
    assert payload["warnings"] == [
        {
            "projection": "pipeline_job_direct",
            "model_id": None,
            "error_type": "projection_fault",
            "reason": "projection_fault",
        }
    ]
    assert "AKIAEXAMPLE" not in json.dumps(payload)
    # The authority append committed: replay sees the bound row and one event.
    assert held_row(repository)["status"] == "submitted"
    assert len(bind_events(repository.root)) == 1

    before = journal_bytes(repository.root)
    assert _invoke(entrypoint, cli_args(repository.root, held)) == 2
    assert "refused: not_held" in capsys.readouterr().err
    assert journal_bytes(repository.root) == before
    assert len(bind_events(repository.root)) == 1


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_a_tilde_journal_root_resolves_through_the_journal_root_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    home = tmp_path / "home"
    repository = held_repository(home / "probe", monkeypatch)
    relative = repository.root.relative_to(home)
    monkeypatch.setenv("HOME", str(home))

    args = cli_args(repository.root, held_row(repository), **{"--journal-root": f"~/{relative}"})
    assert _invoke(entrypoint, args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["journal_root"] == str(repository.root.resolve())
    assert held_row(repository)["status"] == "submitted"
