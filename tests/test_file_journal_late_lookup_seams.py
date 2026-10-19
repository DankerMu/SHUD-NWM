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

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

import services.orchestrator.file_journal_candidates as file_journal_candidates
import services.orchestrator.file_journal_paths as file_journal_paths
import services.orchestrator.file_journal_retry_helpers as file_journal_retry_helpers
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


def test_journal_segment_lineage_reads_through_the_facade_stat_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[object] = []

    def signature_stub(path: object) -> tuple[int, int, int]:
        calls.append(path)
        return (1, 1, 1)

    monkeypatch.setattr(file_orchestration_journal, "_stat_signature", signature_stub)

    result = file_journal_paths._require_journal_segment_lineage(
        tmp_path / "journal" / "gfs" / "2026071200.1.jsonl",
        root=tmp_path,
        cycle_segment="2026071200",
        segment_index=1,
    )

    assert result is None
    assert calls == [tmp_path / "journal" / "gfs" / "2026071200.jsonl"]


def test_discovered_files_walker_reads_through_the_facade_stat_primitives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "d"
    directory.mkdir()
    (directory / "a.json").write_text("{}", encoding="utf-8")
    signature = file_orchestration_journal._stat_signature(directory)
    counts: dict[str, int] = {}

    def counting(name: str, real: Callable[..., object]) -> Callable[..., object]:
        def proxy(*args: object, **kwargs: object) -> object:
            counts[name] = counts.get(name, 0) + 1
            return real(*args, **kwargs)

        return proxy

    monkeypatch.setattr(
        file_orchestration_journal,
        "_stat_signature",
        counting("_stat_signature", file_orchestration_journal._stat_signature),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "stat_no_follow",
        counting("stat_no_follow", file_orchestration_journal.stat_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "list_directory_no_follow_limited",
        counting("list_directory_no_follow_limited", file_orchestration_journal.list_directory_no_follow_limited),
    )

    found = list(
        file_journal_paths._iter_discovered_files(
            directory,
            root=tmp_path,
            suffix=".json",
            recursive=False,
            max_files=10,
            max_depth=2,
            strict_disappearance=True,
            expected_root_signature=signature,
        )
    )

    assert found == [directory / "a.json"]
    assert counts == {"_stat_signature": 4, "stat_no_follow": 2, "list_directory_no_follow_limited": 1}


def test_retention_surface_walker_reads_through_the_facade_stat_primitives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = tmp_path / "journal"
    directory.mkdir()
    budget = file_orchestration_journal._RetentionDiscoveryBudget(limit=10)
    counts: dict[str, int] = {}

    def counting(name: str, real: Callable[..., object]) -> Callable[..., object]:
        def proxy(*args: object, **kwargs: object) -> object:
            counts[name] = counts.get(name, 0) + 1
            return real(*args, **kwargs)

        return proxy

    monkeypatch.setattr(
        file_orchestration_journal,
        "stat_no_follow",
        counting("stat_no_follow", file_orchestration_journal.stat_no_follow),
    )
    monkeypatch.setattr(
        file_orchestration_journal,
        "list_directory_no_follow_limited",
        counting("list_directory_no_follow_limited", file_orchestration_journal.list_directory_no_follow_limited),
    )

    found = list(
        file_journal_paths._iter_retention_surface_files(
            directory,
            root=tmp_path,
            surface="journal",
            max_files=10,
            max_depth=3,
            budget=budget,
        )
    )

    assert found == []
    assert counts == {"stat_no_follow": 1, "list_directory_no_follow_limited": 1}


def test_journal_record_constructor_reads_through_the_facade_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def clock_stub() -> datetime:
        calls.append(None)
        return datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    monkeypatch.setattr(file_orchestration_journal, "_utcnow", clock_stub)

    record = file_journal_retry_helpers._journal_record_for_write(
        "hydro_run",
        {},
        source_id="gfs",
        cycle_time=datetime(2026, 1, 1, tzinfo=UTC),
        model_id=None,
        sequence=1,
    )

    assert len(calls) == 1
    assert record["created_at"] == "2030-01-02T03:04:05Z"


def test_private_recovery_record_constructor_reads_through_the_facade_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def clock_stub() -> datetime:
        calls.append(None)
        return datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)

    monkeypatch.setattr(file_orchestration_journal, "_utcnow", clock_stub)

    record = file_journal_retry_helpers._private_runtime_root_recovery_record(
        {"event_id": "e1", "entity_id": "job_x", "details": {"workspace_dir": "runs/r"}},
        source_id="gfs",
        cycle_time=datetime(2026, 1, 1, tzinfo=UTC),
        model_id=None,
    )

    assert len(calls) == 1
    assert record["created_at"] == "2030-01-02T03:04:05Z"
    assert record["candidates"] == [{"path": ["details"], "value": {"workspace_dir": "runs/r"}}]
