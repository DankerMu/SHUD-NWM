from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    publish_state_snapshot_index,
)
from services.orchestrator.run_tree_copyback import RunTreeCopybackError, copyback_run_trees


def _write_run(root: Path, run_id: str, *, output_text: str = "q\n") -> None:
    run = root / "runs" / run_id
    (run / "input").mkdir(parents=True)
    (run / "output").mkdir()
    (run / "logs").mkdir()
    (run / "input" / "manifest.json").write_text(
        (
            '{"run_id":"'
            + run_id
            + '","model":{"model_package_uri":"s3://nhms/models/basins_heihe_shud/v1/package/"}}\n'
        ),
        encoding="utf-8",
    )
    (run / "input" / "forcing_domain_handoff.json").write_text(
        (
            '{"forcing_package_uri":'
            '"s3://nhms/forcing/gfs/2026062700/basins_heihe_vbasins/basins_heihe_shud"}\n'
        ),
        encoding="utf-8",
    )
    (run / "output" / "q.rivqdown.csv").write_text(output_text, encoding="utf-8")
    (run / "logs" / "shud_stdout.log").write_text("ok\n", encoding="utf-8")
    forcing = root / "forcing" / "gfs" / "2026062700" / "basins_heihe_vbasins" / "basins_heihe_shud"
    forcing.mkdir(parents=True)
    (forcing / "forcing_package.json").write_text("{}\n", encoding="utf-8")
    model = root / "models" / "basins_heihe_shud" / "v1"
    (model / "package").mkdir(parents=True)
    (model / "manifest.json").write_text("{}\n", encoding="utf-8")


def test_copyback_run_trees_replaces_stale_target_tree(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud", output_text="new\n")
    stale = copyback_root / "runs" / "fcst_gfs_2026062700_basins_heihe_shud" / "output"
    stale.mkdir(parents=True)
    (stale / "old.csv").write_text("old\n", encoding="utf-8")

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
    )

    assert summary is not None
    assert summary["status"] == "copied"
    assert summary["run_ids"] == ["fcst_gfs_2026062700_basins_heihe_shud"]
    target = copyback_root / "runs" / "fcst_gfs_2026062700_basins_heihe_shud"
    assert (target / "input" / "manifest.json").is_file()
    assert (target / "output" / "q.rivqdown.csv").read_text(encoding="utf-8") == "new\n"
    assert not (target / "output" / "old.csv").exists()
    assert (
        copyback_root
        / "forcing"
        / "gfs"
        / "2026062700"
        / "basins_heihe_vbasins"
        / "basins_heihe_shud"
        / "forcing_package.json"
    ).is_file()
    assert (copyback_root / "models" / "basins_heihe_shud" / "v1" / "manifest.json").is_file()
    assert {tree["object_key"] for tree in summary["referenced_trees"]} == {
        "forcing/gfs/2026062700/basins_heihe_vbasins/basins_heihe_shud",
        "models/basins_heihe_shud/v1",
    }


def test_copyback_run_trees_copies_extra_state_index_object(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud", output_text="new\n")
    state_index = object_root / "scheduler" / "state-index" / "index-last.json"
    state_index.parent.mkdir(parents=True)
    publish_state_snapshot_index(
        [],
        state_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, tzinfo=UTC),
    )

    previous_umask = os.umask(0o077)
    try:
        summary = copyback_run_trees(
            object_store_root=object_root,
            copyback_root=copyback_root,
            run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
            extra_object_keys=["scheduler/state-index/index-last.json"],
        )
    finally:
        os.umask(previous_umask)

    assert summary is not None
    extra = summary["extra_objects"]
    assert len(extra) == 1
    assert extra[0]["object_key"] == "scheduler/state-index/index-last.json"
    assert extra[0]["merge"]["merged_entry_count"] == 0
    assert '"schema_version": "nhms.scheduler.file_state_snapshot_index.v1"' in (
        copyback_root / "scheduler" / "state-index" / "index-last.json"
    ).read_text(encoding="utf-8")


def test_state_index_copyback_merges_split_root_checkpoint_only_in_private(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    store = LocalObjectStore(object_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    shared_content = _valid_state_bytes(b"shared")
    private_uri = store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", private_content)
    shared_uri = store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z")],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [_state_entry("shared-state", shared_uri, shared_content, "2026-06-27T00:00:00Z")],
        destination_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )

    assert not (copyback_root / "states/gfs/model_a/shared/state.cfg.ic").exists()

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
        extra_object_keys=["scheduler/state-index/index-last.json"],
    )

    assert summary is not None
    payload = json.loads(destination_index.read_text())
    assert {entry["state_id"] for entry in payload["entries"]} == {"private-state", "shared-state"}
    assert summary["extra_objects"][0]["merge"]["merged_entry_count"] == 2
    assert summary["extra_objects"][0]["merge"]["checkpoint_copied_count"] == 2
    assert summary["extra_objects"][0]["merge"]["checkpoint_reused_count"] == 0
    copied_checkpoint = copyback_root / "states/gfs/model_a/private/state.cfg.ic"
    assert copied_checkpoint.read_bytes() == private_content
    assert (copyback_root / "states/gfs/model_a/shared/state.cfg.ic").read_bytes() == shared_content
    assert copied_checkpoint.stat().st_mode & 0o777 == 0o664
    assert copied_checkpoint.parent.stat().st_mode & 0o777 == 0o775
    repository = FileStateSnapshotIndexRepository(
        index_uri=str(destination_index),
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
    )
    entries, _header, _preimage = repository.validated_entries_for_renewal()
    assert {entry["state_id"] for entry in entries} == {"private-state", "shared-state"}


def test_state_index_copyback_checkpoint_failure_preserves_shared_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    private_store = LocalObjectStore(object_root, "s3://nhms")
    shared_store = LocalObjectStore(copyback_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    shared_content = _valid_state_bytes(b"shared")
    private_uri = private_store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", private_content)
    private_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    shared_uri = shared_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z")],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [_state_entry("shared-state", shared_uri, shared_content, "2026-06-27T00:00:00Z")],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    before = destination_index.read_bytes()

    def fail_checkpoint(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise StateManagerError("checkpoint copy failed")

    monkeypatch.setattr(state_manager_module, "_copyback_state_checkpoint", fail_checkpoint)
    with pytest.raises(RunTreeCopybackError) as error_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=copyback_root,
            run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
            extra_object_keys=["scheduler/state-index/index-last.json"],
        )

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED"
    assert destination_index.read_bytes() == before
    assert not (copyback_root / "states/gfs/model_a/private/state.cfg.ic").exists()


def test_state_index_copyback_split_root_checksum_failure_preserves_shared_index(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    private_store = LocalObjectStore(object_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    stale_shared_content = _valid_state_bytes(b"stale-shared")
    expected_shared_content = _valid_state_bytes(b"expected-shared")
    private_uri = private_store.write_bytes_atomic(
        "states/gfs/model_a/private/state.cfg.ic",
        private_content,
    )
    shared_uri = private_store.write_bytes_atomic(
        "states/gfs/model_a/shared/state.cfg.ic",
        stale_shared_content,
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z")],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [
            _state_entry(
                "shared-state",
                shared_uri,
                expected_shared_content,
                "2026-06-27T00:00:00Z",
            )
        ],
        destination_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
        verify_objects=False,
    )
    before = destination_index.read_bytes()

    with pytest.raises(RunTreeCopybackError) as error_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=copyback_root,
            run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
            extra_object_keys=["scheduler/state-index/index-last.json"],
        )

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED"
    assert "state_snapshot_index_object_checksum_mismatch" in error_info.value.details["error"]
    assert destination_index.read_bytes() == before
    assert not (copyback_root / "states").exists()


def test_state_index_copyback_serializes_against_refresh_publisher_without_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    private_store = LocalObjectStore(object_root, "s3://nhms")
    shared_store = LocalObjectStore(copyback_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    shared_content = _valid_state_bytes(b"shared")
    private_uri = private_store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", private_content)
    private_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    shared_uri = shared_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    private_entry = _state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z")
    shared_entry = _state_entry("shared-state", shared_uri, shared_content, "2026-06-27T00:00:00Z")
    publish_state_snapshot_index(
        [private_entry],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [shared_entry],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )
    entered = threading.Event()
    release = threading.Event()
    real_copy_checkpoint = state_manager_module._copyback_state_checkpoint
    copyback_errors: list[BaseException] = []

    def pause_checkpoint(*args: object, **kwargs: object) -> str:
        result = real_copy_checkpoint(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=10)
        return result

    def run_copyback() -> None:
        try:
            copyback_run_trees(
                object_store_root=object_root,
                copyback_root=copyback_root,
                run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
                extra_object_keys=["scheduler/state-index/index-last.json"],
            )
        except BaseException as error:  # pragma: no cover - asserted below
            copyback_errors.append(error)

    monkeypatch.setattr(state_manager_module, "_copyback_state_checkpoint", pause_checkpoint)
    copyback_thread = threading.Thread(target=run_copyback)
    copyback_thread.start()
    assert entered.wait(timeout=10)

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            [shared_entry],
            destination_index,
            object_store_root=copyback_root,
            object_store_prefix="s3://nhms",
            generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
        )
    release.set()
    copyback_thread.join(timeout=10)

    assert "provider_already_running" in str(error_info.value)
    assert not copyback_thread.is_alive()
    assert not copyback_errors
    payload = json.loads(destination_index.read_text())
    assert {entry["state_id"] for entry in payload["entries"]} == {"private-state", "shared-state"}


def _valid_state_bytes(seed: bytes) -> bytes:
    minute = 27_000_000.0 + (int.from_bytes(seed[:4].ljust(4, b"\x00"), "big") % 1000)
    return (
        f"2\t1\t{minute:.6f}\n"
        "1\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "2\t0.1\t0.1\t0.1\t0.1\t0.1\n"
        "1\t0.5\n"
    ).encode()


def _state_entry(state_id: str, uri: str, content: bytes, valid_time: str) -> dict[str, object]:
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": f"run-{state_id}",
        "source_id": "gfs",
        "valid_time": valid_time,
        "state_uri": uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "created_at": valid_time,
    }


def test_copyback_run_trees_rejects_unsafe_run_id(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()

    with pytest.raises(RunTreeCopybackError) as exc_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=tmp_path / "shared-object-store",
            run_ids=["../escape"],
        )

    assert exc_info.value.code == "OBJECT_STORE_COPYBACK_UNSAFE_RUN_ID"
