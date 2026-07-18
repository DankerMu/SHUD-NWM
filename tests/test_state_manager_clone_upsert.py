from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
    StateSnapshot,
)


def test_clone_upsert_retry_preserves_usable_flag() -> None:
    source = inspect.getsource(PsycopgStateSnapshotRepository.upsert_state_snapshot)
    assert "usable_flag = EXCLUDED.usable_flag" in source
    assert "usable_flag = false" not in source


def test_file_index_satisfies_state_clone_lookup_protocol(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    store = LocalObjectStore(object_root, "s3://nhms")
    index_path = object_root / "scheduler" / "state-index.json"
    content = b"2\t1\t27000000.0\n1\t0.1\t0.1\t0.1\t0.1\t0.1\n2\t0.1\t0.1\t0.1\t0.1\t0.1\n1\t0.5\n"
    state_uri = store.write_bytes_atomic("states/gfs/model_a/2026070600/state.cfg.ic", content)
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=datetime(2026, 7, 6, 1, tzinfo=UTC),
        create_missing=True,
    )
    snapshot = StateSnapshot(
        state_id="state_gfs_model_a_2026070600",
        model_id="model_a",
        run_id="fcst_gfs_2026070512_model_a",
        valid_time=datetime(2026, 7, 6, tzinfo=UTC),
        state_uri=state_uri,
        checksum=f"sha256:{sha256_bytes(content)}",
        usable_flag=True,
        source_id="gfs",
        cycle_id="gfs_2026070512",
        lead_hours=12,
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )
    repository.upsert_state_snapshot(snapshot)

    assert repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="gfs",
        valid_time=snapshot.valid_time,
        lead_hours=12,
    ) == snapshot
    assert repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="gfs",
        valid_time=snapshot.valid_time,
        lead_hours=6,
    ) is None
    assert repository.get_latest_state_before(
        model_id="model_a",
        source_id="gfs",
        before_time=datetime(2026, 7, 6, 12, tzinfo=UTC),
    ) == snapshot
