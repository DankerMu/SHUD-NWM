"""Planning, liveness, and receipt contracts for scheduler-journal retention."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import scheduler_journal_archive as archive
from services.orchestrator import scheduler_journal_retention as retention
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.scheduler_journal_retention_fixtures import (
    NOW,
    OLD_CYCLE,
    _bytes,
    _config,
    _fixture_path,
    _frontier,
    _job,
    _record,
    _reservation,
    _seed_cycle,
    _write_json,
    _write_jsonl,
)

SchedulerJournalRetentionConfig = retention.SchedulerJournalRetentionConfig
ReceiptReservation = retention.ReceiptReservation
FrontierReadResult = retention.FrontierReadResult
reserve_receipt = retention.reserve_receipt

def test_default_dry_run_plans_complete_gfs_cycle_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    members = _seed_cycle(config.journal_root, continuation=True, pipeline_event=True)
    direct_job = _job("gfs", OLD_CYCLE)
    direct = _write_json(
        config.journal_root / "pipeline-jobs" / f"{direct_job['job_id']}.json",
        _record("gfs", OLD_CYCLE, direct_job),
    )
    index = _write_json(_fixture_path(config, tmp_path / "state-index.json"), {"authority": "state"})
    before = {path: _bytes(path) for path in [*members, direct, index]}

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["counts"]["planned"] == 1
    row = payload["cycles"][0]
    assert row["status"] == "planned"
    assert row["member_count"] == 4
    assert set(row["members"]) == {
        "latest/gfs/2026050100/model_a.json",
        "journal/gfs/2026050100.jsonl",
        "journal/gfs/2026050100.1.jsonl",
        "pipeline-events/gfs/2026050100.jsonl",
    }
    assert not (config.archive_root / "gfs" / "2026050100").exists()
    assert {path: _bytes(path) for path in before} == before


@pytest.mark.parametrize("source_id", ["gfs", "IFS"])
def test_enforce_archives_exact_hot_cycle_and_preserves_sibling_authority(tmp_path: Path, source_id: str) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    members = _seed_cycle(config.journal_root, source_id=source_id, continuation=True, pipeline_event=True)
    direct_job = _job(source_id, OLD_CYCLE)
    direct = _write_json(
        config.journal_root / "pipeline-jobs" / f"{direct_job['job_id']}.json",
        _record(source_id, OLD_CYCLE, direct_job),
    )
    inventory = _write_json(config.journal_root / "reconcile-inventory" / "anchor.json", {"authority": "inventory"})
    index = _write_json(_fixture_path(config, tmp_path / "state-index.json"), {"authority": "state"})
    before = {path: _bytes(path) for path in [direct, inventory, index]}

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["counts"]["archived"] == 1
    row = payload["cycles"][0]
    assert row["status"] == "archived"
    archive_path = config.archive_root / source_id / "2026050100" / "bundle" / archive.ARCHIVE_NAME
    manifest_path = archive_path.with_name(archive.MANIFEST_NAME)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["archive_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    assert [member["path"] for member in manifest["members"]] == sorted(row["members"])
    assert all(not path.exists() for path in members)
    assert {path: _bytes(path) for path in before} == before


def test_reserved_unbound_and_incomplete_accepted_master_remain_live(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    reserved = _job("gfs", OLD_CYCLE, status="reserved")
    paths = _seed_cycle(config.journal_root, job=reserved)
    incomplete_cycle = OLD_CYCLE - timedelta(hours=12)
    incomplete = _job("gfs", incomplete_cycle, status="succeeded", accepted=True)
    incomplete["candidate_projections"] = []
    _seed_cycle(config.journal_root, cycle=incomplete_cycle, job=incomplete)
    # The accepted-master projection row is intentionally old authority: its
    # malformed legacy form must block only its own cycle, not weaken the
    # reserved/unbound assertion above.

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert any(row["reason"] == "live_row" for row in payload["cycles"])
    assert all(path.exists() for path in paths)


def test_released_identity_blocked_row_without_inventory_anchor_remains_live(tmp_path: Path) -> None:
    from tests.test_file_orchestration_journal import _released_identity_blocked_master

    config = _config(tmp_path, enabled=True, dry_run=False)
    # Build the real current-contract release through the owner lifecycle. Its
    # inventory anchor was removed by the release itself, while cycle journal
    # authority remains and is the only retention source needed here.
    repository, record = _released_identity_blocked_master(config.journal_root.parent)
    released = repository.get_pipeline_job(str(record["job_id"]))
    assert released is not None
    cycle_time = datetime.fromisoformat(str(released["cycle_time"]).replace("Z", "+00:00"))
    source_id = str(released["source_id"])
    hot_member = config.journal_root / "journal" / source_id / f"{cycle_time.strftime('%Y%m%d%H')}.jsonl"

    payload = retention.run_retention(
        config, now=NOW.replace(year=2027), frontier=_frontier(None), receipt_reservation=_reservation(config)
    )

    assert payload["cycles"][0]["reason"] == "live_row"
    assert hot_member.exists()
    assert not any((config.journal_root / "reconcile-inventory").glob("*.json"))


@pytest.mark.parametrize(
    ("frontier", "expected"),
    [
        (_frontier(OLD_CYCLE), "pipeline_frontier_exempt"),
        (retention.FrontierReadResult(status="unavailable", reason="receipt_stale"), "receipt_stale"),
        (_frontier(None), "planned"),
    ],
)
def test_frontier_contract_is_fail_closed_or_explicitly_allows_null(
    tmp_path: Path,
    frontier: retention.FrontierReadResult,
    expected: str,
) -> None:
    config = _config(tmp_path)
    paths = _seed_cycle(config.journal_root)

    payload = retention.run_retention(config, now=NOW, frontier=frontier)

    row = payload["cycles"][0]
    assert expected in {row["reason"], row["status"]}
    assert all(path.exists() for path in paths)
    if expected == "receipt_stale":
        assert payload["preflight_blockers"] == ["receipt_stale"]


def test_invalid_safety_window_blocks_every_cycle(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    config = retention.SchedulerJournalRetentionConfig(**{**config.__dict__, "retention_days": 1})
    paths = _seed_cycle(config.journal_root)

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["preflight_blockers"] == ["scheduler_window_invalid"]
    assert payload["counts"]["archived"] == 0
    assert all(path.exists() for path in paths)


def test_discovery_budget_is_aggregate_and_one_over_blocks_all_removal(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False, max_files=8)
    first = _seed_cycle(config.journal_root, cycle=OLD_CYCLE)
    second = _seed_cycle(config.journal_root, cycle=OLD_CYCLE - timedelta(hours=12))

    payload = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert payload["discovery"]["status"] == "ok"
    assert payload["counts"]["archived"] == 2
    config = _config(tmp_path / "one-over", enabled=True, dry_run=False, max_files=8)
    first = _seed_cycle(config.journal_root, cycle=OLD_CYCLE)
    second = _seed_cycle(config.journal_root, cycle=OLD_CYCLE - timedelta(hours=12))
    _write_json(config.journal_root / "latest" / "gfs" / "2026043012" / "extra.json", {"foreign": True})

    blocked = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))

    assert blocked["discovery"]["status"] == "blocked"
    assert blocked["counts"]["archived"] == 0
    assert all(path.exists() for path in [*first, *second])


def test_malformed_symlink_nonregular_unrecognised_and_gapped_members_block(tmp_path: Path) -> None:
    for kind in ("malformed", "symlink", "fifo", "unrecognised", "gapped"):
        config = _config(tmp_path / kind, enabled=True, dry_run=False)
        paths = _seed_cycle(config.journal_root)
        stamp = "2026050100"
        if kind == "malformed":
            (config.journal_root / "journal" / "gfs" / f"{stamp}.jsonl").write_text("not-json\n", encoding="utf-8")
        elif kind == "symlink":
            target = config.journal_root / "latest" / "gfs" / stamp / "model_a.json"
            target.unlink()
            target.symlink_to("/etc/passwd")
        elif kind == "fifo":
            fifo = config.journal_root / "journal" / "gfs" / f"{stamp}.jsonl"
            fifo.unlink()
            os.mkfifo(fifo)
        elif kind == "unrecognised":
            (config.journal_root / "journal" / "gfs" / "unexpected.txt").write_text("x", encoding="utf-8")
        else:
            _write_jsonl(config.journal_root / "journal" / "gfs" / f"{stamp}.2.jsonl", [])

        payload = retention.run_retention(
            config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
        )

        assert payload["counts"]["archived"] == 0, kind
        assert any(path.exists() or path.is_symlink() for path in paths), kind


def test_lock_contention_skips_without_wait(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
    repository = FileOrchestrationJournalRepository(config.journal_root)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with repository.open_retention_cycle(source_id="gfs", cycle_time=OLD_CYCLE) as window:
            assert window.status == "locked"
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        payload = retention.run_retention(
            config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config)
        )
    finally:
        release.set()
        thread.join(timeout=5)

    assert payload["cycles"][0]["reason"] == "in_flight"
    assert all(path.exists() for path in paths)


def test_matching_archive_retries_partial_cleanup_and_conflict_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
    repository = FileOrchestrationJournalRepository(config.journal_root)
    original = repository._remove_retention_members_unlocked
    calls = 0

    def partial(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 1:
            return type(original(*args, **kwargs))(status="partial", reason="injected")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, "_remove_retention_members_unlocked", partial)
    monkeypatch.setattr(retention, "FileOrchestrationJournalRepository", lambda *_args, **_kwargs: repository)
    first = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert first["cycles"][0]["status"] == "partial"
    monkeypatch.setattr(repository, "_remove_retention_members_unlocked", original)

    # Simulate a real partial unlink: retain one manifest-bound member for the
    # retry rather than merely returning a partial result after full cleanup.
    _seed_cycle(config.journal_root)
    second = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert second["cycles"][0]["status"] == "archived"
    assert all(not path.exists() for path in paths)

    config = _config(tmp_path / "conflict", enabled=True, dry_run=False)
    paths = _seed_cycle(config.journal_root)
    archive_dir = config.archive_root / "gfs" / "2026050100"
    archive_dir.mkdir(parents=True)
    (archive_dir / archive.ARCHIVE_NAME).write_bytes(b"not-an-archive")
    _write_json(archive_dir / archive.MANIFEST_NAME, {"schema_version": archive.MANIFEST_SCHEMA_VERSION})
    monkeypatch.setattr(retention, "FileOrchestrationJournalRepository", FileOrchestrationJournalRepository)
    conflict = retention.run_retention(config, now=NOW, frontier=_frontier(), receipt_reservation=_reservation(config))
    assert conflict["cycles"][0]["reason"] == "archive_conflict"
    assert all(path.exists() for path in paths)
