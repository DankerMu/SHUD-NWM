"""Late-lookup seams of the file-journal siblings (entropy-burndown-2026-10).

Tests patch names on ``services.orchestrator.file_orchestration_journal``.  A
function that moved into a ``file_journal_*`` sibling only sees such a patch
when it reads the name off the facade module at call time; a copy bound by
import (at the sibling's top level, or from the name's original owner module)
keeps calling the real function.  Each case patches the facade with a counting
stub and calls the moved function directly, so an import-bound copy shows up as
a zero count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import services.orchestrator.file_journal_candidates as file_journal_candidates
import services.orchestrator.file_orchestration_journal as file_orchestration_journal


def test_run_manifest_identity_reads_through_the_facade_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path))
    monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
    calls: list[object] = []

    def read_stub(path: object, **kwargs: object) -> bytes:
        calls.append(path)
        return b'{"model": {"model_package_checksum": "c"}}'

    monkeypatch.setattr(file_orchestration_journal, "read_bytes_limited_no_follow", read_stub)

    identity = file_journal_candidates._run_manifest_model_package_identity(
        {"run_manifest_uri": "runs/r/manifest.json"}
    )

    assert len(calls) == 1
    assert identity is not None
    assert identity["model_package_checksum"] == "c"


def test_candidate_row_identity_reads_through_the_facade_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def evidence_stub(job: object) -> dict[str, object]:
        calls.append(job)
        return {"init_state_identities": [{"model_id": "m", "init_state_id": "s"}]}

    monkeypatch.setattr(file_orchestration_journal, "accepted_submit_candidate_immutable_evidence", evidence_stub)

    identity = file_journal_candidates._candidate_row_self_bound_identity({}, model_id="m")

    assert len(calls) == 1
    assert identity == {"model_id": "m", "init_state_id": "s"}
