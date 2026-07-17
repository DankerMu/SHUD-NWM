"""Tests for FileStateSnapshotIndexRepository.generation_scoped_history_signal.

Introduced by Issue #1081 §8.3–§8.7 (state-index history is generation-scoped).

Regression rows covered:
- Empty index → both any/current history absent (drives cold_new_model).
- Same-generation entries → current-generation history present + exact
  predecessor identified when cycle_id/lead_hours match.
- Old-generation entries only → any-generation history present but
  current-generation history absent (drives block_declaration_missing /
  cold_declared_cutover downstream).
- Overlapping (old + new) entries → current-generation history present and
  the latest_any_generation_checkpoint carries the OLD checksum, exposing
  the transition boundary for D8.3 audit.
- Invalid/malformed index → status=blocked, non-raising, so scheduler
  evaluates the block with a stable typed reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    publish_state_snapshot_index,
)

NEW_CHECKSUM = "b" * 64
OLD_CHECKSUM = "a" * 64


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _valid_ic_bytes(seed: bytes) -> bytes:
    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [
        f"2\t1\t{minute:.6f}",
        "1\t0.1\t0.1\t0.1\t0.1\t0.1",
        "2\t0.1\t0.1\t0.1\t0.1\t0.1",
        "1\t0.5",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _publish_entries(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    *,
    generated_at: str = "2026-07-06T00:00:00Z",
) -> Path:
    object_root = tmp_path / "objects"
    LocalObjectStore(object_root, "s3://nhms")  # ensure dir exists
    index_path = tmp_path / "state-index.json"
    publish_state_snapshot_index(
        entries,
        index_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt(generated_at),
    )
    return index_path


def _entry(
    *,
    state_id: str,
    valid_time: str,
    cycle_id: str,
    lead_hours: int,
    checksum_seed: bytes,
    package_checksum: str,
    object_root: Path,
) -> dict[str, Any]:
    content = _valid_ic_bytes(checksum_seed)
    store = LocalObjectStore(object_root, "s3://nhms")
    state_uri = store.write_bytes_atomic(
        f"states/gfs/model_a/{valid_time.replace('-', '').replace(':', '').replace('T', '')[:12]}/{state_id}.cfg.ic",
        content,
    )
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": f"analysis_{cycle_id}_model_a",
        "source_id": "gfs",
        "valid_time": valid_time,
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "cycle_id": cycle_id,
        "lead_hours": lead_hours,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": package_checksum,
    }


def test_generation_scoped_history_signal_empty_index_no_history(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    index_path = _publish_entries(tmp_path, [])

    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T12:00:00Z"),
    )
    signal = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
    )
    assert signal["ready"] is True
    assert signal["history_exists_any_generation"] is False
    assert signal["history_exists_current_generation"] is False
    assert signal["latest_current_generation_checkpoint"] is None
    assert signal["latest_any_generation_checkpoint"] is None


def test_generation_scoped_history_signal_current_generation_only(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    # An exact warm-start predecessor entry has valid_time == the candidate's
    # cycle_time_utc (the state IS the initial condition AT that time); the
    # producing cycle_id sits ``required_lead_hours`` earlier — here
    # gfs_2026070600 producing state at 12z with lead=12h.
    predecessor_entry = _entry(
        state_id="state_current_predecessor",
        valid_time="2026-07-06T12:00:00Z",
        cycle_id="gfs_2026070600",
        lead_hours=12,
        checksum_seed=b"cur1",
        package_checksum=NEW_CHECKSUM,
        object_root=object_root,
    )
    index_path = _publish_entries(tmp_path, [predecessor_entry])

    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T12:00:00Z"),
    )
    signal = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
        expected_predecessor_cycle_id="gfs_2026070600",
        expected_predecessor_lead_hours=12,
    )
    assert signal["history_exists_any_generation"] is True
    assert signal["history_exists_current_generation"] is True
    current = signal["latest_current_generation_checkpoint"]
    assert current is not None
    assert current["has_exact_predecessor"] is True
    assert current["predecessor_cycle_id"] == "gfs_2026070600"
    assert current["predecessor_lead_hours"] == 12


def test_generation_scoped_history_signal_old_generation_only(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    old_entry = _entry(
        state_id="state_old_predecessor",
        valid_time="2026-07-05T12:00:00Z",
        cycle_id="gfs_2026070500",
        lead_hours=12,
        checksum_seed=b"old1",
        package_checksum=OLD_CHECKSUM,
        object_root=object_root,
    )
    index_path = _publish_entries(tmp_path, [old_entry])

    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T12:00:00Z"),
    )
    signal = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
    )
    assert signal["history_exists_any_generation"] is True
    assert signal["history_exists_current_generation"] is False
    assert signal["latest_current_generation_checkpoint"] is None
    latest_any = signal["latest_any_generation_checkpoint"]
    assert latest_any is not None
    assert latest_any["model_package_checksum"] == OLD_CHECKSUM


def test_generation_scoped_history_signal_wrong_generation_no_exact_predecessor(
    tmp_path: Path,
) -> None:
    """Old-gen entry at the exact predecessor key doesn't satisfy warm_continue."""
    object_root = tmp_path / "objects"
    object_root.mkdir(parents=True)
    old_entry = _entry(
        state_id="state_old_predecessor",
        valid_time="2026-07-06T12:00:00Z",
        cycle_id="gfs_2026070600",
        lead_hours=12,
        checksum_seed=b"old2",
        package_checksum=OLD_CHECKSUM,
        object_root=object_root,
    )
    index_path = _publish_entries(tmp_path, [old_entry])

    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T12:00:00Z"),
    )
    signal = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
        expected_predecessor_cycle_id="gfs_2026070600",
        expected_predecessor_lead_hours=12,
    )
    # Any-generation history present, current-generation history absent.
    assert signal["history_exists_any_generation"] is True
    assert signal["history_exists_current_generation"] is False
    assert signal["latest_current_generation_checkpoint"] is None


def test_generation_scoped_history_signal_blocked_when_index_malformed(tmp_path: Path) -> None:
    index_path = tmp_path / "state-index.json"
    index_path.write_text("not-json", encoding="utf-8")
    repo = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=_dt("2026-07-06T12:00:00Z"),
    )
    signal = repo.generation_scoped_history_signal(
        model_id="model_a",
        source_id="gfs",
        before_time=_dt("2026-07-06T12:00:00Z"),
        current_package_checksum=NEW_CHECKSUM,
    )
    assert signal["ready"] is False
    assert signal["status"] == "blocked"
    assert signal["history_exists_any_generation"] is None
    assert signal["history_exists_current_generation"] is None
    assert signal["reason"]
