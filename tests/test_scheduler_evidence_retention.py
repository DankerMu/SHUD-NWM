"""Requirement-driven unit tests for `scripts/node22_scheduler_evidence_retention.py`.

These tests cover the 10 Spec Scenarios from
`openspec/changes/instrument-node22-scheduler-pass-timing/specs/scheduler-evidence-retention/spec.md`.
They land as SUB-9 of Epic #858 (issue #867).

Fixtures: only pytest built-ins (`tmp_path`, `monkeypatch`, `capsys`).
Determinism: `now` is fixed to `2026-07-05T12:00:00Z` and mtimes are set via
`os.utime` — never `time.sleep()` and never patching `datetime.now`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node22_scheduler_evidence_retention as retention


def _config(
    root: Path,
    *,
    retention_days: int = 90,
    max_mb: int = 512,
    receipt_retention_days: int = 180,
    whitelist_globs: tuple[str, ...] = (),
) -> retention.SchedulerEvidenceRetentionConfig:
    """Build a `SchedulerEvidenceRetentionConfig` directly (skip argparse)."""
    return retention.SchedulerEvidenceRetentionConfig(
        evidence_root=root,
        retention_days=retention_days,
        max_bytes=max_mb * 1024 * 1024,
        receipt_retention_days=receipt_retention_days,
        whitelist_globs=whitelist_globs,
        summary_path=None,
    )


def _write_file(path: Path, *, size_bytes: int, mtime: datetime) -> Path:
    """Write a fixed-size file and set its mtime deterministically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size_bytes)
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def test_age_based_deletion_under_cap(tmp_path: Path) -> None:
    """§7.1 — files older than retention_days are deleted; younger files kept."""
    old_name = "scheduler_2026040100_abc123def456.json"
    young_name = "scheduler_2026070100_bbb111111111.json"
    old_path = _write_file(
        tmp_path / old_name,
        size_bytes=1024,
        mtime=NOW - timedelta(days=100),
    )
    young_path = _write_file(
        tmp_path / young_name,
        size_bytes=1024,
        mtime=NOW - timedelta(days=4),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    assert payload["deleted_count"] == 1
    assert len(payload["deleted_paths"]) == 1
    deleted = payload["deleted_paths"][0]
    assert deleted["path"].endswith(old_name)
    assert deleted["pass"] == "age"
    # mtime is a valid ISO-Z string reflecting the pre-deletion mtime (100 days ago).
    parsed_mtime = datetime.strptime(deleted["mtime"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert parsed_mtime == (NOW - timedelta(days=100)).replace(microsecond=0)
    assert not old_path.exists()
    assert young_path.exists()


def test_size_based_eviction_after_age(tmp_path: Path) -> None:
    """§7.2 — after age pass, oldest-first eviction runs until under the size cap."""
    # 3 files, 500 KB each, all younger than 90d retention. Total = 1_536_000 bytes.
    # max_mb=1 → cap = 1_048_576. Evict oldest (30d) → 1_024_000 ≤ cap → stop.
    file_30d = _write_file(
        tmp_path / "scheduler_2026060500_aaa000000001.json",
        size_bytes=500 * 1024,
        mtime=NOW - timedelta(days=30),
    )
    file_29d = _write_file(
        tmp_path / "scheduler_2026060600_aaa000000002.json",
        size_bytes=500 * 1024,
        mtime=NOW - timedelta(days=29),
    )
    file_28d = _write_file(
        tmp_path / "scheduler_2026060700_aaa000000003.json",
        size_bytes=500 * 1024,
        mtime=NOW - timedelta(days=28),
    )

    payload = retention.run_retention(_config(tmp_path, max_mb=1), now=NOW)

    assert payload["deleted_count"] == 1
    assert len(payload["deleted_paths"]) == 1
    deleted = payload["deleted_paths"][0]
    assert deleted["pass"] == "size"
    assert deleted["path"].endswith("scheduler_2026060500_aaa000000001.json")
    assert not file_30d.exists()
    assert file_29d.exists()
    assert file_28d.exists()


def test_in_flight_write_skipped(tmp_path: Path) -> None:
    """§7.3 — a file with a sibling `.tmp` is skipped as `in-flight`."""
    target = _write_file(
        tmp_path / "scheduler_2026040100_ccc333333333.pre_execution.json",
        size_bytes=1024,
        mtime=NOW - timedelta(days=100),
    )
    tmp_sibling = tmp_path / "scheduler_2026040100_ccc333333333.pre_execution.json.tmp"
    tmp_sibling.write_bytes(b"")

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    assert payload["deleted_count"] == 0
    in_flight = payload["skipped_paths_by_reason"]["in-flight"]
    assert any(entry["path"] == str(target) for entry in in_flight)
    assert target.exists()
    assert tmp_sibling.exists()


def test_safety_window_skipped(tmp_path: Path) -> None:
    """§7.4 — files younger than 1 h are skipped as `safety-window`."""
    target = _write_file(
        tmp_path / "scheduler_2026070500_ddd444444444.json",
        size_bytes=1024,
        mtime=NOW - timedelta(minutes=30),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    assert payload["deleted_count"] == 0
    safety = payload["skipped_paths_by_reason"]["safety-window"]
    assert any(entry["path"] == str(target) for entry in safety)
    assert target.exists()


def test_foreign_file_untouched(tmp_path: Path) -> None:
    """§7.5 — foreign files (e.g. `notes.txt`) are recorded as `unrecognised`."""
    foreign = _write_file(
        tmp_path / "notes.txt",
        size_bytes=32,
        mtime=NOW - timedelta(days=100),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    unrecognised = payload["skipped_paths_by_reason"]["unrecognised"]
    assert any(entry["path"] == str(foreign) for entry in unrecognised)
    assert foreign.exists()


def test_empty_pass_still_emits_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§7.6 — an empty pass still writes a receipt with `deleted_count: 0`."""
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(tmp_path))

    exit_code = retention.main(["--evidence-root", str(tmp_path)])

    assert exit_code == 0
    receipt_dir = tmp_path / "retention"
    assert receipt_dir.is_dir()
    receipts = sorted(receipt_dir.glob("retention-*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["deleted_count"] == 0
    assert payload["total_before_bytes"] == 0
    assert payload["total_after_bytes"] == 0
    assert payload["finished_at"]
    skipped = payload["skipped_paths_by_reason"]
    assert skipped["in-flight"] == []
    assert skipped["safety-window"] == []
    assert skipped["unrecognised"] == []
    # Suppress stdout capture leak between tests.
    capsys.readouterr()


def test_pre_execution_json_subject_to_age_pass(tmp_path: Path) -> None:
    """§7.7 — a `.pre_execution.json` older than retention is deleted alongside its sibling."""
    json_path = _write_file(
        tmp_path / "scheduler_2026040100_eee555555555.json",
        size_bytes=1024,
        mtime=NOW - timedelta(days=100),
    )
    pre_path = _write_file(
        tmp_path / "scheduler_2026040100_eee555555555.pre_execution.json",
        size_bytes=1024,
        mtime=NOW - timedelta(days=100),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    assert payload["deleted_count"] == 2
    deleted_paths = {entry["path"] for entry in payload["deleted_paths"]}
    assert str(json_path) in deleted_paths
    assert str(pre_path) in deleted_paths
    for entry in payload["deleted_paths"]:
        assert entry["pass"] == "age"
    assert not json_path.exists()
    assert not pre_path.exists()


def test_receipt_files_have_longer_window(tmp_path: Path) -> None:
    """§7.8 — receipts follow the 180-day `receipt_retention_days` window."""
    receipt_dir = tmp_path / "retention"
    receipt_dir.mkdir()
    old_receipt = _write_file(
        receipt_dir / "retention-20250101T000000Z.json",
        size_bytes=256,
        mtime=NOW - timedelta(days=200),
    )
    young_receipt = _write_file(
        receipt_dir / "retention-20260601T000000Z.json",
        size_bytes=256,
        mtime=NOW - timedelta(days=34),
    )
    scheduler_file = _write_file(
        tmp_path / "scheduler_2026070100_fff666666666.json",
        size_bytes=1024,
        mtime=NOW - timedelta(days=100),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    assert len(payload["receipt_pass"]) == 1
    assert payload["receipt_pass"][0]["path"] == str(old_receipt)
    assert not old_receipt.exists()
    assert young_receipt.exists()
    scheduler_deleted = [
        entry for entry in payload["deleted_paths"] if entry["path"] == str(scheduler_file)
    ]
    assert len(scheduler_deleted) == 1
    assert scheduler_deleted[0]["pass"] == "age"
    assert payload["policy"]["receipt_retention_days"] == 180


def test_evidence_write_error_json_is_out_of_scope(tmp_path: Path) -> None:
    """§7.9 — `evidence_write_error.json` is never deleted, regardless of age."""
    marker = _write_file(
        tmp_path / "evidence_write_error.json",
        size_bytes=64,
        mtime=NOW - timedelta(days=300),
    )

    payload = retention.run_retention(_config(tmp_path), now=NOW)

    unrecognised = payload["skipped_paths_by_reason"]["unrecognised"]
    assert any(entry["path"] == str(marker) for entry in unrecognised)
    assert marker.exists()
    deleted_paths = {entry["path"] for entry in payload["deleted_paths"]}
    assert str(marker) not in deleted_paths


def test_retention_creates_receipt_subdir_on_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§7.10 — first fire on a fresh evidence root creates `retention/` and writes a receipt."""
    assert not (tmp_path / "retention").exists()
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(tmp_path))

    exit_code = retention.main(["--evidence-root", str(tmp_path)])

    assert exit_code == 0
    receipt_dir = tmp_path / "retention"
    assert receipt_dir.is_dir()
    receipts = sorted(receipt_dir.glob("retention-*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["deleted_count"] == 0
    # Suppress stdout capture leak between tests.
    capsys.readouterr()
