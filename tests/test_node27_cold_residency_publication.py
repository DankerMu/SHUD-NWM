"""Publication, sidecar, and lock-order tests for cold residency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_receipt import (
    ColdReceiptError,
    intent_path_for,
    publish_intent,
    publish_receipt,
    read_intent,
    remove_intent,
)
from packages.common.safe_fs import SafeFilesystemError
from scripts import node27_cold_residency as runner
from tests.cold_residency_fakes import FakeConnection, chunk, complete_relations
from tests.test_node27_cold_residency import _ready

_ROOT = Path(__file__).resolve().parents[1]
_NOW = datetime(2026, 7, 11, 12, tzinfo=UTC)
_INTENT = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.intent.example.json").read_text())
_TERMINAL = json.loads((_ROOT / "schemas/examples/timeseries_cold_residency_receipt.example.json").read_text())


def test_intent_then_public_receipt_then_sidecar_removal(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_intent(sidecar, _INTENT)
    assert sidecar.exists()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    publish_receipt(receipt_path, _INTENT)
    assert json.loads(receipt_path.read_text())["outcome"] == "in_progress"
    publish_receipt(receipt_path, _TERMINAL)
    remove_intent(sidecar)
    assert not sidecar.exists()
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"


def test_publication_failure_does_not_replay_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_intent(sidecar, _INTENT)
    calls = {"count": 0}

    def boom(*_args: object, **_kwargs: object) -> None:
        calls["count"] += 1
        raise SafeFilesystemError("fsync failed", kind="indeterminate")

    monkeypatch.setattr("packages.common.compressed_chunk_cold_receipt.atomic_write_bytes_no_follow", boom)
    with pytest.raises(ColdReceiptError, match="publication"):
        publish_receipt(receipt_path, _TERMINAL)
    assert sidecar.exists()
    assert calls["count"] == 1


def test_symlink_receipt_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "receipt.json"
    alias.symlink_to(target)
    with pytest.raises((ColdReceiptError, SafeFilesystemError)):
        publish_receipt(alias, _TERMINAL)


def test_stale_success_cannot_outrank_sidecar(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    sidecar = intent_path_for(receipt_path)
    publish_receipt(receipt_path, _TERMINAL)
    publish_intent(sidecar, _INTENT)
    parsed = read_intent(sidecar)
    assert parsed["outcome"] == "in_progress"
    assert json.loads(receipt_path.read_text())["outcome"] == "clean"


def test_mixed_unresolved_intent_blocks_new_selection(tmp_path: Path) -> None:
    env = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
    }
    args = runner._parser().parse_args(["--enforce"])
    config = runner.config_from_args(args, env)
    sidecar = intent_path_for(config.receipt_path)
    publish_intent(sidecar, _INTENT)
    connection = FakeConnection()
    item = chunk()
    connection.load_group(item, complete_relations(other_space="nhms_cold"))
    receipt = runner.run_tick(
        _ready(config),
        now_utc=_NOW,
        head_sha="a" * 40,
        connect=lambda: connection,
        fetch_watermark=lambda: _NOW,
    )
    assert receipt["recovery"]["blocked_new_selection"] is True
    assert sidecar.exists()


def test_lifecycle_lock_is_acquired_before_lane_lock() -> None:
    source = (_ROOT / "scripts/node27_cold_residency.py").read_text(encoding="utf-8")
    lifecycle = source.index("acquire_timeseries_lifecycle_lock")
    lane = source.index("acquire_lock(config.lock_path)")
    assert lifecycle < lane
    compression = (_ROOT / "scripts/node27_timeseries_compression.py").read_text(encoding="utf-8")
    assert compression.index("acquire_timeseries_lifecycle_lock") < compression.index("acquire_lock(config.lock_path)")
    retention = (_ROOT / "scripts/node27_timeseries_retention.py").read_text(encoding="utf-8")
    assert retention.index("acquire_timeseries_lifecycle_lock") < retention.index("acquire_lock(config.lock_path)")
    replay = (_ROOT / "scripts/node27_timeseries_decompression_replay.py").read_text(encoding="utf-8")
    assert "timeseries_lifecycle_lock" in replay
