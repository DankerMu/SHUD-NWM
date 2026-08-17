from __future__ import annotations

import errno
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import ProviderPreimage, capture_provider_preimage
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateManagerError,
    publish_state_snapshot_index,
)
from services.orchestrator.run_tree_copyback import RunTreeCopybackError, copyback_run_trees
from tests.test_state_manager import _LockReleaseSeam


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


def _write_direct_grid_run(root: Path, run_id: str) -> None:
    run = root / "runs" / run_id
    (run / "input").mkdir(parents=True)
    (run / "input" / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "model": {
                    "model_package_uri": (
                        "s3://nhms/models/direct_grid_variants/basins_qhh_shud/"
                        "dg-gfs-variant/package/"
                    )
                },
                "forcing_package_uri": (
                    "s3://nhms/forcing/gfs/2026070600/basins_qhh_vbasins/dg-candidate/"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    forcing = root / "forcing/gfs/2026070600/basins_qhh_vbasins/dg-candidate"
    forcing.mkdir(parents=True)
    (forcing / "forcing_package.json").write_text("{}\n", encoding="utf-8")
    package = root / "models/direct_grid_variants/basins_qhh_shud/dg-gfs-variant/package"
    package.mkdir(parents=True)
    (package / "manifest.json").write_text('{"variant":"gfs"}\n', encoding="utf-8")
    (package / "qhh.cfg.para").write_text("model\n", encoding="utf-8")


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


def test_copyback_direct_grid_run_scopes_model_tree_to_exact_variant(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026070600_dg_candidate"
    _write_direct_grid_run(object_root, run_id)

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=[run_id],
    )

    assert summary is not None
    assert {tree["object_key"] for tree in summary["referenced_trees"]} == {
        "forcing/gfs/2026070600/basins_qhh_vbasins/dg-candidate",
        "models/direct_grid_variants/basins_qhh_shud/dg-gfs-variant",
    }
    assert (
        copyback_root
        / "models/direct_grid_variants/basins_qhh_shud/dg-gfs-variant/package/manifest.json"
    ).is_file()


def test_copyback_reuses_matching_immutable_direct_grid_model_tree(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026070600_dg_candidate"
    _write_direct_grid_run(object_root, run_id)
    target_package = (
        copyback_root / "models/direct_grid_variants/basins_qhh_shud/dg-gfs-variant/package"
    )
    target_package.mkdir(parents=True)
    (target_package / "manifest.json").write_text('{"variant":"gfs"}\n', encoding="utf-8")
    marker = target_package / "node27-owned-marker"
    marker.write_text("preserve\n", encoding="utf-8")

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=[run_id],
    )

    assert summary is not None
    model_summary = next(
        tree for tree in summary["referenced_trees"] if tree["object_key"].startswith("models/")
    )
    assert model_summary["status"] == "reused"
    assert model_summary["file_count"] == 0
    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_copyback_rejects_mismatched_immutable_direct_grid_model_tree(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026070600_dg_candidate"
    _write_direct_grid_run(object_root, run_id)
    target_package = (
        copyback_root / "models/direct_grid_variants/basins_qhh_shud/dg-gfs-variant/package"
    )
    target_package.mkdir(parents=True)
    (target_package / "manifest.json").write_text('{"variant":"ifs"}\n', encoding="utf-8")

    with pytest.raises(RunTreeCopybackError) as error_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=copyback_root,
            run_ids=[run_id],
        )

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_MODEL_IDENTITY_MISMATCH"


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


def test_state_index_copyback_merges_split_root_checkpoint_only_in_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    store = LocalObjectStore(object_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    shared_content = _valid_state_bytes(b"shared")
    private_uri = store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", private_content)
    shared_uri = store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    LocalObjectStore(copyback_root, "s3://nhms").write_bytes_atomic(
        "states/gfs/model_a/shared/state.cfg.ic", shared_content
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    private_entry = {
        **_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z"),
        "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
    }
    publish_state_snapshot_index(
        [private_entry],
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
    # #1189 regression lock: a destination-only entry must be neither read nor
    # copied by the merge.  All checkpoint object IO happens inside
    # _copyback_state_checkpoint, so spying on it proves scope, and the shared
    # object's bytes plus mtime prove no write.
    checkpoint_state_ids: list[str] = []
    real_copy_checkpoint = state_manager_module._copyback_state_checkpoint

    def record_checkpoint(entry: dict[str, object], **kwargs: object) -> str:
        checkpoint_state_ids.append(str(entry.get("state_id")))
        return real_copy_checkpoint(entry, **kwargs)

    monkeypatch.setattr(state_manager_module, "_copyback_state_checkpoint", record_checkpoint)
    shared_checkpoint = copyback_root / "states/gfs/model_a/shared/state.cfg.ic"
    shared_before = (shared_checkpoint.read_bytes(), shared_checkpoint.stat().st_mtime_ns)

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
    assert summary["extra_objects"][0]["merge"]["checkpoint_copied_count"] == 1
    assert summary["extra_objects"][0]["merge"]["checkpoint_reused_count"] == 0
    assert checkpoint_state_ids == ["private-state"]
    assert (shared_checkpoint.read_bytes(), shared_checkpoint.stat().st_mtime_ns) == shared_before
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


def test_state_index_copyback_ignores_derived_entry_evidence_for_same_identity(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_state_bytes(b"same-state")
    state_uri = store.write_bytes_atomic("states/gfs/model_a/same/state.cfg.ic", content)
    base_entry = _state_entry("same-state", state_uri, content, "2026-06-27T01:00:00Z")
    source_entry = {
        **base_entry,
        "index_generated_at": "2026-06-27T03:00:00Z",
        "object_evidence": {"checksum_verified": True, "provider": "stale"},
    }
    destination_entry = {
        **base_entry,
        "index_generated_at": "2026-06-27T02:00:00Z",
        "object_evidence": {"checksum_verified": True, "provider": "stale"},
    }
    LocalObjectStore(copyback_root, "s3://nhms").write_bytes_atomic(
        "states/gfs/model_a/same/state.cfg.ic", content
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [source_entry],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [destination_entry],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
        extra_object_keys=["scheduler/state-index/index-last.json"],
    )

    assert summary is not None
    merge = summary["extra_objects"][0]["merge"]
    assert merge["merged_entry_count"] == 1
    # The private entry belongs to an unrelated run, so the scoped copyback
    # copies nothing (#1189 winning-source-entry scope); the shared entry is
    # still republished with its derived evidence stripped.
    assert merge["checkpoint_reused_count"] == 0
    payload = json.loads(destination_index.read_text(encoding="utf-8"))
    assert payload["entries"] == [base_entry]


def test_state_index_copyback_scopes_source_entries_and_materializes_clone(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026062700_basins_heihe_shud"
    _write_run(object_root, run_id)
    store = LocalObjectStore(object_root, "s3://nhms")
    shared_store = LocalObjectStore(copyback_root, "s3://nhms")
    materialized_content = _valid_state_bytes(b"materialized")
    clone_content = _valid_state_bytes(b"clone")
    unrelated_content = _valid_state_bytes(b"unrelated")
    materialized_uri = store.write_bytes_atomic(
        "states/gfs/model_a/materialized/state.cfg.ic", materialized_content
    )
    clone_uri = store.write_bytes_atomic("states/gfs/model_a/clone/state.cfg.ic", clone_content)
    unrelated_uri = store.write_bytes_atomic(
        "states/gfs/model_b/unrelated/state.cfg.ic", unrelated_content
    )
    shared_store.write_bytes_atomic(
        "states/gfs/model_a/materialized/state.cfg.ic", clone_content
    )
    shared_store.write_bytes_atomic("states/gfs/model_a/clone/state.cfg.ic", clone_content)
    shared_store.write_bytes_atomic(
        "states/gfs/model_b/unrelated/state.cfg.ic", unrelated_content
    )
    source_entry = {
        **_state_entry("materialized", materialized_uri, materialized_content, "2026-06-27T01:00:00Z"),
        "run_id": run_id,
        "cloned_from_state_id": None,
    }
    clone_entry = {
        **_state_entry("clone", clone_uri, clone_content, "2026-06-27T01:00:00Z"),
        "run_id": "fcst_gfs_2026062612_basins_heihe_shud",
        "cloned_from_state_id": "predecessor-state",
    }
    unrelated_private = {
        **_state_entry("unrelated-private", unrelated_uri, unrelated_content, "2026-06-27T01:00:00Z"),
        "model_id": "model_b",
        "run_id": "unrelated-private-run",
    }
    unrelated_shared = {
        **unrelated_private,
        "state_id": "unrelated-shared",
        "run_id": "unrelated-shared-run",
    }
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [source_entry, unrelated_private],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [clone_entry, unrelated_shared],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=[run_id],
        extra_object_keys=["scheduler/state-index/index-last.json"],
    )

    assert summary is not None
    merge = summary["extra_objects"][0]["merge"]
    assert merge["source_entry_count"] == 1
    assert merge["authoritative_run_count"] == 1
    assert merge["checkpoint_replaced_count"] == 1
    payload = json.loads(destination_index.read_text(encoding="utf-8"))
    by_model = {entry["model_id"]: entry for entry in payload["entries"]}
    assert by_model["model_a"]["state_id"] == "materialized"
    assert by_model["model_b"]["state_id"] == "unrelated-shared"
    assert shared_store.read_bytes("states/gfs/model_a/materialized/state.cfg.ic") == materialized_content


def test_state_index_copyback_replay_validates_each_index_against_its_own_root(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026062700_basins_heihe_shud"
    _write_run(object_root, run_id)
    private_store = LocalObjectStore(object_root, "s3://nhms")
    shared_store = LocalObjectStore(copyback_root, "s3://nhms")
    private_content = _valid_state_bytes(b"replayed-state")
    shared_content = _valid_state_bytes(b"old-state")
    state_key = "states/gfs/model_a/replayed/state.cfg.ic"
    state_uri = private_store.write_bytes_atomic(state_key, private_content)
    shared_store.write_bytes_atomic(state_key, shared_content)
    source_entry = {
        **_state_entry("replayed-state", state_uri, private_content, "2026-06-27T01:00:00Z"),
        "run_id": run_id,
    }
    destination_entry = {
        **_state_entry("old-state", state_uri, shared_content, "2026-06-27T01:00:00Z"),
        "run_id": run_id,
    }
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [source_entry],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [destination_entry],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
    )

    summary = copyback_run_trees(
        object_store_root=object_root,
        copyback_root=copyback_root,
        run_ids=[run_id],
        extra_object_keys=["scheduler/state-index/index-last.json"],
    )

    assert summary is not None
    merge = summary["extra_objects"][0]["merge"]
    assert merge["source_entry_count"] == 1
    assert merge["checkpoint_replaced_count"] == 1
    assert shared_store.read_bytes(state_key) == private_content
    payload = json.loads(destination_index.read_text(encoding="utf-8"))
    assert payload["entries"] == [source_entry]


def test_state_index_copyback_same_timestamp_semantic_conflict_fails_closed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_state_bytes(b"conflict")
    state_uri = store.write_bytes_atomic("states/gfs/model_a/conflict/state.cfg.ic", content)
    source_entry = {
        **_state_entry("same-state", state_uri, content, "2026-06-27T01:00:00Z"),
        "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
    }
    destination_entry = {**source_entry, "run_id": "different-real-run"}
    LocalObjectStore(copyback_root, "s3://nhms").write_bytes_atomic(
        "states/gfs/model_a/conflict/state.cfg.ic", content
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [source_entry],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
    )
    publish_state_snapshot_index(
        [destination_entry],
        destination_index,
        object_store_root=copyback_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
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
    assert "state_snapshot_index_copyback_conflict" in error_info.value.details["error"]
    # No self-described phase at all: every raise point of this reason is
    # before the destination compare-and-swap, so it keeps the fail-closed
    # code, and the reason is now legible under that code too (#1364).
    assert error_info.value.details["error_reason"] == "state_snapshot_index_copyback_conflict"
    assert destination_index.read_bytes() == before
    assert (
        copyback_root / "states/gfs/model_a/conflict/state.cfg.ic"
    ).read_bytes() == content


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
        [
            {
                **_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z"),
                # The copied run owns this entry, so it is the one checkpoint
                # this scoped copyback must carry (#1189).
                "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
            }
        ],
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
    # An error carrying neither reason nor phase still gets the key, so the
    # details shape is the same under both codes.
    assert error_info.value.details["error_reason"] is None
    assert destination_index.read_bytes() == before
    assert not (copyback_root / "states/gfs/model_a/private/state.cfg.ic").exists()


def test_state_index_copyback_lock_release_failure_reports_commit_uncertain_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1193: the provider lock releases only after the destination
    # compare-and-swap, so the natural copyback path must not label this failure
    # with the fail-closed code -- that would push a shared index that really is
    # committed back into the "nothing happened" bucket and reverse the
    # operator's bisection.  The distinct code is what carries the difference
    # into the pipeline event.
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    _write_run(object_root, "fcst_gfs_2026062700_basins_heihe_shud")
    store = LocalObjectStore(object_root, "s3://nhms")
    private_content = _valid_state_bytes(b"private")
    shared_content = _valid_state_bytes(b"shared")
    private_uri = store.write_bytes_atomic("states/gfs/model_a/private/state.cfg.ic", private_content)
    shared_uri = store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    LocalObjectStore(copyback_root, "s3://nhms").write_bytes_atomic(
        "states/gfs/model_a/shared/state.cfg.ic", shared_content
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [
            {
                **_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z"),
                "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
            }
        ],
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
    seam = _LockReleaseSeam(destination_index)
    monkeypatch.setattr(provider_atomic_module, "fcntl", seam)

    with pytest.raises(RunTreeCopybackError) as error_info:
        copyback_run_trees(
            object_store_root=object_root,
            copyback_root=copyback_root,
            run_ids=["fcst_gfs_2026062700_basins_heihe_shud"],
            extra_object_keys=["scheduler/state-index/index-last.json"],
        )

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN"
    assert error_info.value.code != "OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED"
    assert error_info.value.details["error_reason"] == "provider_lock_release_failed"
    assert error_info.value.details["object_key"] == "scheduler/state-index/index-last.json"
    assert seam.failed_releases == 1
    # The merge did commit: the shared index holds both entries and the winning
    # checkpoint object landed.
    published = json.loads(destination_index.read_text(encoding="utf-8"))["entries"]
    assert {entry["state_id"] for entry in published} == {"private-state", "shared-state"}
    assert (copyback_root / "states/gfs/model_a/private/state.cfg.ic").read_bytes() == private_content


class _StateIndexCopybackFixture:
    """The lock-release test's two-root fixture, reused by the CAS injections.

    ``destination_exists=False`` is the bootstrap shape: the shared index is
    absent, so the provider's post-CAS failure has no previous content to roll
    back to and the destination necessarily keeps the merged bytes.
    """

    def __init__(self, tmp_path: Path, *, destination_exists: bool = True) -> None:
        self.run_id = "fcst_gfs_2026062700_basins_heihe_shud"
        self.object_root = tmp_path / "object-store"
        self.copyback_root = tmp_path / "shared-object-store"
        _write_run(self.object_root, self.run_id)
        private_store = LocalObjectStore(self.object_root, "s3://nhms")
        self.private_content = _valid_state_bytes(b"private")
        self.shared_content = _valid_state_bytes(b"shared")
        private_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/private/state.cfg.ic", self.private_content
        )
        shared_uri = private_store.write_bytes_atomic(
            "states/gfs/model_a/shared/state.cfg.ic", self.shared_content
        )
        LocalObjectStore(self.copyback_root, "s3://nhms").write_bytes_atomic(
            "states/gfs/model_a/shared/state.cfg.ic", self.shared_content
        )
        self.source_index = self.object_root / "scheduler/state-index/index-last.json"
        self.destination_index = self.copyback_root / "scheduler/state-index/index-last.json"
        publish_state_snapshot_index(
            [
                {
                    **_state_entry("private-state", private_uri, self.private_content, "2026-06-27T01:00:00Z"),
                    "run_id": self.run_id,
                }
            ],
            self.source_index,
            object_store_root=self.object_root,
            object_store_prefix="s3://nhms",
            generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
        )
        if destination_exists:
            publish_state_snapshot_index(
                [_state_entry("shared-state", shared_uri, self.shared_content, "2026-06-27T00:00:00Z")],
                self.destination_index,
                object_store_root=self.copyback_root,
                object_store_prefix="s3://nhms",
                generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
            )

    def run(self) -> None:
        copyback_run_trees(
            object_store_root=self.object_root,
            copyback_root=self.copyback_root,
            run_ids=[self.run_id],
            extra_object_keys=["scheduler/state-index/index-last.json"],
        )

    def published_state_ids(self) -> set[str]:
        payload = json.loads(self.destination_index.read_text(encoding="utf-8"))
        return {entry["state_id"] for entry in payload["entries"]}


class _DestinationWriteSeam:
    """Fails the destination compare-and-swap with a chosen failure kind.

    The helper is shared with every other provider write, so the injection is
    filtered to the destination index path.  ``kind="indeterminate"`` lets the
    real replace land first, which is what makes "the shared index already
    holds the merged entries" an assertable fact rather than a claim.
    """

    def __init__(self, destination: Path, *, kind: str) -> None:
        self._destination = destination
        self._kind = kind
        self.writes = 0

    def __call__(self, path: Path, content: bytes, **kwargs: object) -> Path:
        if Path(path) != self._destination:
            return atomic_write_bytes_no_follow(path, content, **kwargs)
        self.writes += 1
        if self._kind == "indeterminate":
            atomic_write_bytes_no_follow(path, content, **kwargs)
            raise SafeFilesystemError(f"directory fsync failed for {path}", kind="indeterminate")
        raise SafeFilesystemError(f"failed to write {path}", kind=self._kind)


class _PostCasReadBackSeam:
    """Fails the read-back that follows a real destination compare-and-swap.

    ``capture_provider_preimage`` also serves the source-side snapshot read, so
    the injection is filtered to the destination index path, and it is armed
    only once a destination write has returned: on that path the captures run
    as pre-merge snapshot, CAS preimage, then post-CAS read-back, and only the
    last one is past the commit.  It fires once so the provider's own
    post-rollback capture still runs for real and can verify the restore.
    """

    def __init__(self, destination: Path) -> None:
        self._destination = destination
        self._armed = False
        self.failures = 0

    def write(self, path: Path, content: bytes, **kwargs: object) -> Path:
        result = atomic_write_bytes_no_follow(path, content, **kwargs)
        if Path(path) == self._destination:
            self._armed = True
        return result

    def capture(self, path: Path, **kwargs: object) -> ProviderPreimage:
        if self._armed and self.failures == 0 and Path(path) == self._destination:
            self.failures += 1
            raise OSError(errno.EIO, "Input/output error")
        return capture_provider_preimage(path, **kwargs)


def test_state_index_copyback_replace_uncertain_reports_commit_uncertain_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #1364: the destination compare-and-swap's own uncertain family reaches
    # the copyback rewrapped as a StateManagerError, never as a bare
    # ProviderAtomicError, so a carrier-typed discriminator misses it and files
    # a merge that really did commit under the fail-closed code -- the exact
    # inversion of the operator bisection #1193 exists to protect.
    fixture = _StateIndexCopybackFixture(tmp_path)
    seam = _DestinationWriteSeam(fixture.destination_index, kind="indeterminate")
    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", seam)

    with pytest.raises(RunTreeCopybackError) as error_info:
        fixture.run()

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN"
    assert error_info.value.code != "OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED"
    assert error_info.value.details["error_reason"] == "provider_replace_uncertain"
    assert error_info.value.details["object_key"] == "scheduler/state-index/index-last.json"
    assert seam.writes == 1
    # The commit is a fact, not an inference: the replace landed before the
    # durability confirmation failed.
    assert fixture.published_state_ids() == {"private-state", "shared-state"}


def test_state_index_copyback_postread_failure_reports_commit_uncertain_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bootstrap shape: with no previous destination content the provider cannot
    # roll back, so `provider_postread_failed` is raised with the merged bytes
    # left in place.
    fixture = _StateIndexCopybackFixture(tmp_path, destination_exists=False)
    seam = _PostCasReadBackSeam(fixture.destination_index)
    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", seam.write)
    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", seam.capture)

    with pytest.raises(RunTreeCopybackError) as error_info:
        fixture.run()

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN"
    assert error_info.value.details["error_reason"] == "provider_postread_failed"
    assert seam.failures == 1
    assert fixture.published_state_ids() == {"private-state"}


def test_state_index_copyback_verified_rollback_reports_commit_uncertain_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rollback verifies (phase "postcommit"), yet the merged bytes were
    # briefly visible to concurrent readers, so this stays commit-uncertain --
    # the same verdict the replay tool gives by excluding the reason from its
    # pre-commit allowlist.  The operator's next step is unchanged: check the
    # shared entry_count, which here must show the batch absent.
    fixture = _StateIndexCopybackFixture(tmp_path)
    before = fixture.destination_index.read_bytes()
    seam = _PostCasReadBackSeam(fixture.destination_index)
    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", seam.write)
    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", seam.capture)

    with pytest.raises(RunTreeCopybackError) as error_info:
        fixture.run()

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_COMMIT_UNCERTAIN"
    assert error_info.value.details["error_reason"] == "provider_restored_previous"
    assert seam.failures == 1
    assert fixture.destination_index.read_bytes() == before
    assert fixture.published_state_ids() == {"shared-state"}


def test_state_index_copyback_precommit_replace_failure_stays_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the discriminator: a rewrapped StateManagerError whose
    # phase is "precommit" must keep the fail-closed code.  A discriminator
    # written as "any phase at all" would pass every uncertain case above while
    # flipping this one, which is precisely the reason the replay tool refuses
    # on -- the two operator surfaces would disagree again.
    fixture = _StateIndexCopybackFixture(tmp_path)
    before = fixture.destination_index.read_bytes()
    seam = _DestinationWriteSeam(fixture.destination_index, kind="io")
    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", seam)

    with pytest.raises(RunTreeCopybackError) as error_info:
        fixture.run()

    assert error_info.value.code == "OBJECT_STORE_COPYBACK_STATE_INDEX_FAILED"
    # `provider_replace_failed` is outside the state manager's reason remap, so
    # it survives verbatim into the event details.
    assert error_info.value.details["error_reason"] == "provider_replace_failed"
    assert seam.writes == 1
    assert fixture.destination_index.read_bytes() == before


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
    LocalObjectStore(copyback_root, "s3://nhms").write_bytes_atomic(
        "states/gfs/model_a/shared/state.cfg.ic", stale_shared_content
    )
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    publish_state_snapshot_index(
        [
            {
                # The copied run's own entry records a checksum that diverges
                # from its private-root object: source-side verification still
                # covers every source entry and fails closed (#1189
                # must-preserve; only destination-side object existence was
                # narrowed).
                **_state_entry("private-state", private_uri, expected_shared_content, "2026-06-27T01:00:00Z"),
                "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
            }
        ],
        source_index,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=datetime(2026, 6, 27, 2, tzinfo=UTC),
        verify_objects=False,
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
        object_store_root=copyback_root,
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
    assert error_info.value.details["error_reason"] == "state_snapshot_index_object_checksum_mismatch"
    assert destination_index.read_bytes() == before
    assert (
        copyback_root / "states/gfs/model_a/shared/state.cfg.ic"
    ).read_bytes() == stale_shared_content
    assert not (copyback_root / "states/gfs/model_a/private/state.cfg.ic").exists()


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
    private_entry = {
        **_state_entry("private-state", private_uri, private_content, "2026-06-27T01:00:00Z"),
        "run_id": "fcst_gfs_2026062700_basins_heihe_shud",
    }
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


def test_state_index_copyback_holds_source_lock_until_checkpoint_copy_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    run_id = "fcst_gfs_2026062700_basins_heihe_shud"
    _write_run(object_root, run_id)
    private_store = LocalObjectStore(object_root, "s3://nhms")
    shared_store = LocalObjectStore(copyback_root, "s3://nhms")
    first_content = _valid_state_bytes(b"first")
    second_content = _valid_state_bytes(b"second")
    shared_content = _valid_state_bytes(b"shared")
    first_uri = private_store.write_bytes_atomic("states/gfs/model_a/first/state.cfg.ic", first_content)
    second_uri = private_store.write_bytes_atomic("states/gfs/model_a/second/state.cfg.ic", second_content)
    shared_uri = shared_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    private_store.write_bytes_atomic("states/gfs/model_a/shared/state.cfg.ic", shared_content)
    source_index = object_root / "scheduler/state-index/index-last.json"
    destination_index = copyback_root / "scheduler/state-index/index-last.json"
    first_entry = {
        **_state_entry("first-state", first_uri, first_content, "2026-06-27T01:00:00Z"),
        "run_id": run_id,
    }
    second_entry = {
        **_state_entry("second-state", second_uri, second_content, "2026-06-27T02:00:00Z"),
        "run_id": run_id,
    }
    shared_entry = _state_entry("shared-state", shared_uri, shared_content, "2026-06-27T00:00:00Z")
    publish_state_snapshot_index(
        [first_entry],
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
    publisher_errors: list[BaseException] = []

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
                run_ids=[run_id],
                extra_object_keys=["scheduler/state-index/index-last.json"],
            )
        except BaseException as error:  # pragma: no cover - asserted below
            copyback_errors.append(error)

    def publish_replacement() -> None:
        try:
            publish_state_snapshot_index(
                [second_entry],
                source_index,
                object_store_root=object_root,
                object_store_prefix="s3://nhms",
                generated_at=datetime(2026, 6, 27, 3, tzinfo=UTC),
            )
        except BaseException as error:  # pragma: no cover - asserted below
            publisher_errors.append(error)

    monkeypatch.setattr(state_manager_module, "_copyback_state_checkpoint", pause_checkpoint)
    copyback_thread = threading.Thread(target=run_copyback)
    copyback_thread.start()
    assert entered.wait(timeout=10), copyback_errors
    publisher_thread = threading.Thread(target=publish_replacement)
    publisher_thread.start()
    publisher_thread.join(timeout=10)
    assert not publisher_thread.is_alive()
    assert len(publisher_errors) == 1
    assert getattr(publisher_errors[0], "reason", None) == "provider_already_running"

    release.set()
    copyback_thread.join(timeout=10)

    assert not copyback_thread.is_alive()
    assert not copyback_errors
    destination_payload = json.loads(destination_index.read_text())
    assert {entry["state_id"] for entry in destination_payload["entries"]} == {
        "first-state",
        "shared-state",
    }
    publisher_errors.clear()
    publish_replacement()
    assert not publisher_errors
    source_payload = json.loads(source_index.read_text())
    assert {entry["state_id"] for entry in source_payload["entries"]} == {"second-state"}


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
