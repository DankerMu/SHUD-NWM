"""#2541: file state-index writes must not pay a per-entry filesystem walk.

node-22 array state saves serialize on the index lock; each hold re-validates the
whole index (load, then again in publish).  Validation used to construct a
``LocalObjectStore`` -- which walks the object root no-follow from ``/`` -- once
per entry, so on the NFS-backed production index every hold issued O(entries)
directory opens.  These tests pin the per-pass walk, keep the concurrency and
failure semantics of the locked read-modify-publish, and keep the typed error
attribution the per-entry walk used to give.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest

from packages.common import object_store as object_store_module
from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import ProviderAtomicError
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    StateSnapshot,
    publish_state_snapshot_index,
)

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)
PREFIX = "s3://nhms"


def _seed_entry(index: int) -> dict[str, Any]:
    model_id = f"basins_m{index % 20}_shud"
    valid_time = NOW - timedelta(hours=12 * (index // 20 + 1))
    stamp = valid_time.strftime("%Y%m%d%H")
    cycle_id = f"gfs_{(valid_time - timedelta(hours=12)).strftime('%Y%m%d%H')}"
    return {
        "state_id": f"state_GFS_{model_id}_{stamp}_{cycle_id}_f012",
        "model_id": model_id,
        "run_id": f"fcst_{cycle_id}_{model_id}",
        "source_id": "GFS",
        "valid_time": valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_uri": f"{PREFIX}/states/GFS/{model_id}/{stamp}/{cycle_id}/f012/state.cfg.ic",
        "checksum": "0" * 64,
        "usable_flag": True,
        "cycle_id": cycle_id,
        "lead_hours": 12,
        "model_package_version": f"{PREFIX}/models/{model_id}/package/",
        "model_package_checksum": "f" * 64,
    }


def _seed_index(tmp_path: Path, entry_count: int) -> tuple[Path, Path]:
    object_root = (tmp_path / "object-store").resolve()
    object_root.mkdir(parents=True)
    index_path = object_root / "scheduler" / "state-index" / "index-last.json"
    index_path.parent.mkdir(parents=True)
    publish_state_snapshot_index(
        [_seed_entry(index) for index in range(entry_count)],
        str(index_path),
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        generated_at=NOW,
        verify_objects=False,
    )
    return object_root, index_path


def _new_snapshot(object_root: Path, label: str) -> StateSnapshot:
    content = f"state-ic-{label}".encode()
    key = f"states/GFS/new_{label}/2026092212/gfs_2026092200/f012/state.cfg.ic"
    state_uri = LocalObjectStore(object_root, PREFIX).write_bytes_atomic(key, content)
    return StateSnapshot(
        state_id=f"state_GFS_new_{label}_2026092212_gfs_2026092200_f012",
        model_id=f"new_{label}",
        run_id=f"fcst_gfs_2026092200_new_{label}",
        valid_time=NOW,
        state_uri=state_uri,
        checksum=sha256_bytes(content),
        usable_flag=False,
        source_id="GFS",
        cycle_id="gfs_2026092200",
        lead_hours=12,
    )


def _repository(
    object_root: Path,
    index_path: Path,
    *,
    create_missing: bool = True,
) -> FileStateSnapshotIndexRepository:
    return FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        now=NOW,
        create_missing=create_missing,
    )


def _count_root_walks(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    walks: list[Path] = []
    original = object_store_module.ensure_directory_no_follow

    def counting(path: Path, *args: Any, **kwargs: Any) -> Path:
        walks.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(object_store_module, "ensure_directory_no_follow", counting)
    return walks


def _locked_write_walks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_count: int) -> int:
    object_root, index_path = _seed_index(tmp_path, entry_count)
    snapshot = _new_snapshot(object_root, "walk")
    repository = _repository(object_root, index_path)
    walks = _count_root_walks(monkeypatch)
    repository.upsert_state_snapshot(snapshot)
    repository.set_usable_flag(state_id=snapshot.state_id, usable_flag=True)
    monkeypatch.undo()
    return len(walks)


def test_locked_index_write_walks_object_root_per_pass_not_per_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small = _locked_write_walks(tmp_path / "small", monkeypatch, 10)
    large = _locked_write_walks(tmp_path / "large", monkeypatch, 400)

    # upsert + set_usable_flag = 2 lock holds x (load validate + publish validate)
    # plus the changed entry's own check; none of it may scale with index size.
    assert large == small
    assert large <= 12


def test_unlocked_index_read_walks_object_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root, index_path = _seed_index(tmp_path, 300)
    repository = _repository(object_root, index_path, create_missing=False)
    walks = _count_root_walks(monkeypatch)

    # A miss still validates the whole 300-entry index (and verifies no object).
    assert repository.get_state_snapshot("state_GFS_absent_2026092212_gfs_2026092200_f012") is None
    assert repository.state_index_evidence()["entry_count"] == 300
    assert len(walks) == 1


def _published_entries(index_path: Path) -> dict[str, dict[str, Any]]:
    return {entry["state_id"]: entry for entry in json.loads(index_path.read_text())["entries"]}


def _upsert_in_process(object_root: str, index_path: str, label: str) -> str:
    root = Path(object_root)
    repository = _repository(root, Path(index_path))
    snapshot = _new_snapshot(root, label)
    repository.upsert_state_snapshot(snapshot)
    repository.set_usable_flag(state_id=snapshot.state_id, usable_flag=True)
    return snapshot.state_id


def test_parallel_process_upserts_all_land_under_the_index_lock(tmp_path: Path) -> None:
    object_root, index_path = _seed_index(tmp_path, 200)
    labels = [f"p{index}" for index in range(8)]

    with get_context("spawn").Pool(processes=len(labels)) as pool:
        state_ids = pool.starmap(
            _upsert_in_process,
            [(str(object_root), str(index_path), label) for label in labels],
        )

    by_state_id = _published_entries(index_path)
    assert len(by_state_id) == 200 + len(labels)
    for state_id in state_ids:
        assert by_state_id[state_id]["usable_flag"] is True


def test_failed_publish_leaves_index_unchanged_and_next_upsert_lands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root, index_path = _seed_index(tmp_path, 50)
    before = index_path.read_bytes()
    repository = _repository(object_root, index_path)
    failing = _new_snapshot(object_root, "fail")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise ProviderAtomicError("provider_destination_unsafe", phase="replace")

    monkeypatch.setattr(state_manager_module, "atomic_replace_provider_bytes", refuse)
    with pytest.raises(StateManagerError) as error_info:
        repository.upsert_state_snapshot(failing)
    monkeypatch.undo()

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_write_failed"
    assert index_path.read_bytes() == before

    landed = _new_snapshot(object_root, "ok")
    repository.upsert_state_snapshot(landed)
    state_ids = set(_published_entries(index_path))
    assert len(state_ids) == 51
    assert landed.state_id in state_ids
    assert failing.state_id not in state_ids


@pytest.mark.parametrize(
    "state_uri",
    [
        f"{PREFIX}/states/GFS/m/%2F/state.cfg.ic",
        f"{PREFIX}/../outside/state.cfg.ic",
        "s3://other-bucket/states/GFS/m/state.cfg.ic",
    ],
)
def test_unsafe_entry_keeps_its_own_typed_field_after_cached_root(tmp_path: Path, state_uri: str) -> None:
    object_root, index_path = _seed_index(tmp_path, 5)
    entries = [_seed_entry(index) for index in range(5)]
    entries[3]["state_uri"] = state_uri

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            entries,
            str(index_path),
            object_store_root=object_root,
            object_store_prefix=PREFIX,
            generated_at=NOW,
            verify_objects=False,
        )

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_object_unsafe_uri"
    assert getattr(error_info.value, "field", "") == "entries[3].state_uri"


def test_unsafe_object_root_fails_closed_on_first_entry(tmp_path: Path) -> None:
    real_root = (tmp_path / "real-root").resolve()
    real_root.mkdir()
    symlinked_root = tmp_path.resolve() / "symlinked-root"
    symlinked_root.symlink_to(real_root, target_is_directory=True)
    index_path = tmp_path.resolve() / "index-last.json"

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            [_seed_entry(index) for index in range(3)],
            str(index_path),
            object_store_root=symlinked_root,
            object_store_prefix=PREFIX,
            generated_at=NOW,
            verify_objects=False,
        )

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_object_unsafe_uri"
    assert getattr(error_info.value, "field", "") == "entries[0].state_uri"
    assert not index_path.exists()


def test_verified_publish_still_reads_every_object_with_cached_root(tmp_path: Path) -> None:
    object_root, index_path = _seed_index(tmp_path, 0)
    snapshots = [_new_snapshot(object_root, f"v{index}") for index in range(3)]
    entries = [state_manager_module._state_index_entry_from_snapshot(snapshot) for snapshot in snapshots]
    entries[2]["checksum"] = "1" * 64

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            entries,
            str(index_path),
            object_store_root=object_root,
            object_store_prefix=PREFIX,
            generated_at=NOW,
            verify_objects=True,
        )

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_object_checksum_mismatch"
    assert getattr(error_info.value, "field", "") == "entries[2].state_uri"
    assert json.loads(index_path.read_text())["entries"] == []
