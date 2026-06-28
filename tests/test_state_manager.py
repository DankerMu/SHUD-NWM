from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.state_snapshots import get_state_manager
from packages.common import state_cli
from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
    FileStateSnapshotIndexRepository,
    StateManager,
    StateManagerError,
    StateSnapshot,
    assess_freshness,
    publish_state_snapshot_index,
    state_snapshot_id,
)
from packages.common.state_qc import MAX_STATE_IC_BYTES


class FakeStateSnapshotRepository:
    def __init__(self) -> None:
        self.snapshots: dict[str, StateSnapshot] = {}
        self.by_model_time: dict[tuple[str, str | None, datetime], str] = {}
        self.qc_results: list[dict[str, Any]] = []

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        return self.snapshots.get(state_id)

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
    ) -> StateSnapshot | None:
        state_id = self.by_model_time.get((model_id, source_id, _dt(valid_time)))
        if state_id is None and source_id is None:
            state_id = self.by_model_time.get((model_id, None, _dt(valid_time)))
        return self.snapshots.get(state_id) if state_id is not None else None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        valid_time = _dt(snapshot.valid_time)
        key = (snapshot.model_id, snapshot.source_id, valid_time)
        existing_state_id = self.by_model_time.get(key)
        if existing_state_id is not None and existing_state_id != snapshot.state_id:
            self.snapshots.pop(existing_state_id, None)
        saved = StateSnapshot(
            state_id=snapshot.state_id,
            model_id=snapshot.model_id,
            run_id=snapshot.run_id,
            valid_time=valid_time,
            state_uri=snapshot.state_uri,
            checksum=snapshot.checksum,
            usable_flag=snapshot.usable_flag,
            created_at=snapshot.created_at or _dt("2026-05-08T00:00:00Z"),
            source_id=snapshot.source_id,
            cycle_id=snapshot.cycle_id,
            lead_hours=snapshot.lead_hours,
            model_package_version=snapshot.model_package_version,
            model_package_checksum=snapshot.model_package_checksum,
            original_shud_filename=snapshot.original_shud_filename,
        )
        self.snapshots[saved.state_id] = saved
        self.by_model_time[(saved.model_id, saved.source_id, saved.valid_time)] = saved.state_id
        return saved

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
        snapshot = self.snapshots.get(state_id)
        if snapshot is None:
            return None
        updated = StateSnapshot(
            state_id=snapshot.state_id,
            model_id=snapshot.model_id,
            run_id=snapshot.run_id,
            valid_time=snapshot.valid_time,
            state_uri=snapshot.state_uri,
            checksum=snapshot.checksum,
            usable_flag=usable_flag,
            created_at=snapshot.created_at,
            source_id=snapshot.source_id,
            cycle_id=snapshot.cycle_id,
            lead_hours=snapshot.lead_hours,
            model_package_version=snapshot.model_package_version,
            model_package_checksum=snapshot.model_package_checksum,
            original_shud_filename=snapshot.original_shud_filename,
        )
        self.snapshots[state_id] = updated
        return updated

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        before = _dt(before_time)
        candidates = [
            snapshot
            for snapshot in self.snapshots.values()
            if snapshot.model_id == model_id and snapshot.usable_flag and snapshot.valid_time <= before
        ]
        return max(candidates, key=lambda snapshot: snapshot.valid_time) if candidates else None

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        items = list(self.snapshots.values())
        if model_id is not None:
            items = [snapshot for snapshot in items if snapshot.model_id == model_id]
        if usable is not None:
            items = [snapshot for snapshot in items if snapshot.usable_flag is usable]
        items.sort(key=lambda snapshot: (snapshot.valid_time, snapshot.state_id), reverse=True)
        page = items[offset : offset + limit]
        return {
            "total_count": len(items),
            "items": [_snapshot_dict(snapshot) for snapshot in page],
            "limit": limit,
            "offset": offset,
        }

    def insert_qc_result(self, record: dict[str, Any]) -> dict[str, Any]:
        saved = {"qc_id": len(self.qc_results) + 1, **record}
        self.qc_results.append(saved)
        return saved


@pytest.fixture
def repository() -> FakeStateSnapshotRepository:
    return FakeStateSnapshotRepository()


@pytest.fixture
def manager(tmp_path: Path, repository: FakeStateSnapshotRepository) -> StateManager:
    return StateManager(repository=repository, object_store=LocalObjectStore(tmp_path))


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


def test_save_state_snapshot_uploads_file_and_uses_expected_state_id(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    ic_file = tmp_path / "run.cfg.ic"
    ic_file.write_bytes(b"state-content")
    valid_time = _dt("2026-04-30T00:00:00Z")

    result = manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_001",
        valid_time=valid_time,
        ic_file_path=ic_file,
    )

    assert result.status == "created"
    assert result.state_id == "state_demo_model_2026043000"
    assert result.state_id == state_snapshot_id("demo_model", valid_time)
    snapshot = repository.snapshots[result.state_id]
    assert snapshot.usable_flag is False
    assert snapshot.state_uri == "states/demo_model/2026043000/state.cfg.ic"
    assert manager.object_store.read_bytes(snapshot.state_uri) == b"state-content"


def test_state_snapshot_qc_pass_sets_usable_true(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    result = _save_ic(tmp_path, manager, content=b"valid-state")

    passed = manager.run_qc(result.state_id)

    assert passed is True
    assert repository.snapshots[result.state_id].usable_flag is True
    assert repository.qc_results[-1]["passed"] is True
    assert repository.qc_results[-1]["checks_json"]["checksum_matches"] is True


def test_state_snapshot_qc_fail_keeps_usable_false(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    result = _save_ic(tmp_path, manager, content=b"valid-state")
    manager.object_store.delete(repository.snapshots[result.state_id].state_uri)

    passed = manager.run_qc(result.state_id)

    assert passed is False
    assert repository.snapshots[result.state_id].usable_flag is False
    assert repository.qc_results[-1]["passed"] is False
    assert repository.qc_results[-1]["checks_json"]["error_code"] == "STATE_FILE_MISSING"


def test_file_state_snapshot_index_strict_lookup_returns_exact_usable_checkpoint(tmp_path: Path) -> None:
    object_store = LocalObjectStore(tmp_path / "objects", "s3://nhms")
    content = _valid_ic_bytes(b"state-index-ready")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"

    receipt = publish_state_snapshot_index(
        [
            {
                "state_id": "state_gfs_model_a_2026052106",
                "model_id": "model_a",
                "run_id": "analysis_gfs_2026052018_model_a",
                "source_id": "gfs",
                "valid_time": "2026-05-21T06:00:00Z",
                "state_uri": state_uri,
                "checksum": f"sha256:{sha256_bytes(content)}",
                "usable_flag": True,
                "cycle_id": "gfs_2026052018",
                "lead_hours": 12,
                "model_package_version": "s3://nhms/models/model_a/package/",
                "model_package_checksum": "package-sha",
            }
        ],
        index_path,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    snapshot = repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="GFS",
        valid_time=_dt("2026-05-21T06:00:00Z"),
    )
    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert receipt["status"] == "published"
    assert snapshot is not None
    assert snapshot.state_id == "state_gfs_model_a_2026052106"
    assert evidence["ready"] is True
    assert evidence["candidate_state"]["init_state_uri"] == state_uri
    assert evidence["candidate_state"]["init_state_lineage"]["lead_hours"] == 12
    assert evidence["state_snapshot_index"]["schema_version"] == FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("index_uri", "object_store_prefix", "expected_index_path"),
    [
        (
            "published://manifests/scheduler/state-index.json",
            "s3://nhms",
            ("published", "manifests/scheduler/state-index.json"),
        ),
        (
            "s3://nhms-prod/scheduler/state-index.json",
            "s3://nhms-prod/scheduler",
            ("objects", "state-index.json"),
        ),
    ],
)
def test_file_state_snapshot_index_object_uri_round_trip(
    tmp_path: Path,
    index_uri: str,
    object_store_prefix: str,
    expected_index_path: tuple[str, str],
) -> None:
    object_root = tmp_path / "objects"
    published_root = tmp_path / "published"
    object_store = LocalObjectStore(object_root, object_store_prefix)
    content = _valid_ic_bytes(b"object-index-round-trip")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    entry = _state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")

    receipt = publish_state_snapshot_index(
        [entry],
        index_uri,
        object_store_root=object_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_root,
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    repository = FileStateSnapshotIndexRepository(
        index_uri,
        object_store_root=object_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_root,
        now=_dt("2026-05-21T12:00:00Z"),
    )
    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )
    root_name, relative_index_path = expected_index_path
    physical_root = published_root if root_name == "published" else object_root

    assert receipt["status"] == "published"
    assert (physical_root / relative_index_path).is_file()
    assert evidence["ready"] is True
    assert evidence["candidate_state"]["init_state_uri"] == state_uri
    updated = repository.set_usable_flag(state_id="state_gfs_model_a_2026052106", usable_flag=False)
    blocked = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert updated is not None
    assert updated.usable_flag is False
    assert blocked["ready"] is False
    assert blocked["reason"] == "state_snapshot_index_checkpoint_unusable"


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("missing_object", "state_snapshot_index_object_missing"),
        ("checksum_mismatch", "state_snapshot_index_object_checksum_mismatch"),
        ("unusable", "state_snapshot_index_checkpoint_unusable"),
        ("wrong_package", "state_snapshot_index_model_package_checksum_mismatch"),
    ],
)
def test_file_state_snapshot_index_fail_closed_cases(
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"state-index-fail")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    entry = {
        "state_id": "state_gfs_model_a_2026052106",
        "model_id": "model_a",
        "run_id": "analysis_gfs_2026052018_model_a",
        "source_id": "gfs",
        "valid_time": "2026-05-21T06:00:00Z",
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": case_name != "unusable",
        "lead_hours": 12,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": "old-package-sha" if case_name == "wrong_package" else "package-sha",
    }
    publish_state_snapshot_index(
        [entry],
        index_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    if case_name == "missing_object":
        (object_root / object_store.normalize_key(state_uri)).unlink()
    elif case_name == "checksum_mismatch":
        (object_root / object_store.normalize_key(state_uri)).write_text("changed\n", encoding="utf-8")

    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )
    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is False
    assert evidence["reason"] == expected_reason
    assert "latest" not in json.dumps(evidence).lower()


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("stale", "state_snapshot_index_stale"),
        ("unsupported_schema", "state_snapshot_index_schema_unsupported"),
        ("malformed_json", "state_snapshot_index_malformed_json"),
        ("checksum_missing", "state_snapshot_index_checksum_missing"),
        ("checksum_mismatch", "state_snapshot_index_checksum_mismatch"),
        ("generated_at_future", "state_snapshot_index_generated_at_future"),
        ("entry_limit", "state_snapshot_index_entry_limit_exceeded"),
        ("wrong_model", "state_snapshot_index_exact_checkpoint_missing"),
        ("wrong_source", "state_snapshot_index_exact_checkpoint_missing"),
        ("wrong_time", "state_snapshot_index_exact_checkpoint_missing"),
    ],
)
def test_file_state_snapshot_index_fail_closed_index_matrix(
    monkeypatch: Any,
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"index-matrix")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    entry = _state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")
    generated_at = "2026-05-21T12:00:00Z"
    repository_now = _dt("2026-05-21T12:00:00Z")

    if case_name == "malformed_json":
        index_path.write_text("{not-json", encoding="utf-8")
    else:
        if case_name == "wrong_model":
            entry["model_id"] = "model_b"
        elif case_name == "wrong_source":
            entry["source_id"] = "ifs"
        elif case_name == "wrong_time":
            entry["valid_time"] = "2026-05-21T12:00:00Z"
        elif case_name == "stale":
            generated_at = "2026-05-01T00:00:00Z"
            repository_now = _dt("2026-05-21T12:00:00Z")
        elif case_name == "generated_at_future":
            generated_at = "2026-05-21T12:06:00Z"
        elif case_name == "entry_limit":
            monkeypatch.setattr(state_manager_module, "MAX_STATE_SNAPSHOT_INDEX_ENTRIES", 0)
        payload: dict[str, Any] = {
            "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
            "generated_at": generated_at,
            "entries": [entry],
        }
        if case_name == "unsupported_schema":
            payload["schema_version"] = "nhms.scheduler.file_state_snapshot_index.v0"
        if case_name != "checksum_missing":
            payload["checksum"] = f"sha256:{_payload_checksum(payload)}"
        if case_name == "checksum_mismatch":
            payload["checksum"] = "sha256:bad"
        index_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=repository_now,
        max_age_hours=1 if case_name == "stale" else 168,
    )
    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is False
    assert evidence["reason"] == expected_reason
    assert "candidate_state" not in evidence
    assert "latest" not in json.dumps(evidence).lower()


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("missing", "state_snapshot_index_missing"),
        ("size", "state_snapshot_index_size_limit_exceeded"),
        ("depth", "state_snapshot_index_json_depth_exceeded"),
        ("nodes", "state_snapshot_index_json_node_limit_exceeded"),
        ("symlink_read", "state_snapshot_index_unreadable"),
    ],
)
def test_file_state_snapshot_index_fail_closed_file_boundaries(
    monkeypatch: Any,
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"index-boundaries")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    if case_name == "missing":
        pass
    elif case_name == "size":
        monkeypatch.setattr(state_manager_module, "MAX_STATE_SNAPSHOT_INDEX_BYTES", 8)
        index_path.write_text("012345678", encoding="utf-8")
    elif case_name == "symlink_read":
        target = tmp_path / "target-state-index.json"
        _write_state_index_payload(
            target,
            [_state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")],
            generated_at="2026-05-21T12:00:00Z",
        )
        index_path.symlink_to(target)
    else:
        entry = _state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")
        payload: dict[str, Any] = {
            "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
            "generated_at": "2026-05-21T12:00:00Z",
            "entries": [entry],
        }
        if case_name == "depth":
            monkeypatch.setattr(state_manager_module, "MAX_STATE_SNAPSHOT_INDEX_JSON_DEPTH", 2)
        else:
            monkeypatch.setattr(state_manager_module, "MAX_STATE_SNAPSHOT_INDEX_JSON_NODES", 2)
        payload["checksum"] = f"sha256:{_payload_checksum(payload)}"
        index_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is False
    assert evidence["reason"] == expected_reason
    assert "candidate_state" not in evidence


def test_file_state_snapshot_index_publish_refuses_symlink_index_target(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"publish-symlink")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    target = tmp_path / "target-state-index.json"
    target.write_text("do-not-overwrite\n", encoding="utf-8")
    index_path = tmp_path / "state-index.json"
    index_path.symlink_to(target)

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            [_state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")],
            index_path,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            generated_at=_dt("2026-05-21T12:00:00Z"),
        )

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_write_failed"
    assert target.read_text(encoding="utf-8") == "do-not-overwrite\n"


def test_file_state_snapshot_index_published_uri_publish_refuses_symlink_target(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    published_root = tmp_path / "published"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"published-symlink")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    symlink_path = published_root / "manifests" / "scheduler" / "state-index.json"
    symlink_path.parent.mkdir(parents=True)
    target = tmp_path / "target-state-index.json"
    target.write_text("do-not-overwrite\n", encoding="utf-8")
    symlink_path.symlink_to(target)

    with pytest.raises(StateManagerError) as error_info:
        publish_state_snapshot_index(
            [_state_index_test_entry(state_uri, content, state_id="state_gfs_model_a_2026052106")],
            "published://manifests/scheduler/state-index.json",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            published_artifact_root=published_root,
            generated_at=_dt("2026-05-21T12:00:00Z"),
        )

    assert getattr(error_info.value, "reason", "") == "state_snapshot_index_write_failed"
    assert target.read_text(encoding="utf-8") == "do-not-overwrite\n"


def test_strict_warm_start_evidence_caches_index_and_verifies_only_exact_objects(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    index_path = tmp_path / "state-index.json"
    entries: list[dict[str, Any]] = []
    for source_id, hour in (("gfs", "06"), ("ifs", "12"), ("era5", "18")):
        content = _valid_ic_bytes(f"{source_id}-lazy-verify".encode("utf-8"))
        state_uri = object_store.write_bytes_atomic(
            f"states/{source_id}/model_a/20260521{hour}/state.cfg.ic",
            content,
        )
        entry = _state_index_test_entry(state_uri, content, state_id=f"state_{source_id}_model_a_20260521{hour}")
        entry["source_id"] = source_id
        entry["valid_time"] = f"2026-05-21T{hour}:00:00Z"
        entries.append(entry)
    publish_state_snapshot_index(
        entries,
        index_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )
    original_read_payload = FileStateSnapshotIndexRepository._read_payload
    payload_reads = {"count": 0}
    object_reads: list[str] = []
    original_read_object = state_manager_module._read_state_object_bytes

    def counting_read_payload(
        self: FileStateSnapshotIndexRepository,
        *,
        allow_empty: bool,
    ) -> tuple[dict[str, Any], bytes]:
        payload_reads["count"] += 1
        return original_read_payload(self, allow_empty=allow_empty)

    def counting_read_object(uri: str, **kwargs: Any) -> bytes:
        object_reads.append(uri)
        return original_read_object(uri, **kwargs)

    monkeypatch.setattr(FileStateSnapshotIndexRepository, "_read_payload", counting_read_payload)
    monkeypatch.setattr(state_manager_module, "_read_state_object_bytes", counting_read_object)

    first = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )
    second = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="ifs",
        valid_time=_dt("2026-05-21T12:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert first["ready"] is True
    assert second["ready"] is True
    assert payload_reads["count"] == 1
    assert object_reads == [entries[0]["state_uri"], entries[1]["state_uri"]]
    assert entries[2]["state_uri"] not in object_reads


@pytest.mark.parametrize("usable_flag", ["false", "0", 1, None])
def test_file_state_snapshot_index_rejects_non_boolean_usable_flag(
    tmp_path: Path,
    usable_flag: Any,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"typed-usable")
    state_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    entry = {
        "state_id": "state_gfs_model_a_2026052106",
        "model_id": "model_a",
        "run_id": "analysis_gfs_2026052018_model_a",
        "source_id": "gfs",
        "valid_time": "2026-05-21T06:00:00Z",
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": usable_flag,
        "lead_hours": 12,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": "package-sha",
    }
    _write_state_index_payload(index_path, [entry], generated_at="2026-05-21T12:00:00Z")
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is False
    assert evidence["reason"] == "state_snapshot_index_usable_flag_invalid"
    assert "candidate_state" not in evidence


@pytest.mark.parametrize(
    ("state_uri", "expected_reason"),
    [
        ("s3://other-bucket/states/gfs/model_a/2026052106/state.cfg.ic", "state_snapshot_index_object_unsafe_uri"),
        ("s3://nhms/states/gfs/model_a/2026052106/state.cfg.ic?token=secret", "state_snapshot_index_object_unsafe_uri"),
        ("s3://user:pass@nhms/states/gfs/model_a/2026052106/state.cfg.ic", "state_snapshot_index_object_unsafe_uri"),
        ("s3://nhms/states/gfs/model_a/2026052106/state.cfg.ic#fragment", "state_snapshot_index_object_unsafe_uri"),
        ("s3://nhms/states/gfs/%2e%2e/model_a/state.cfg.ic", "state_snapshot_index_object_unsafe_uri"),
        ("/tmp/state.cfg.ic", "state_snapshot_index_object_unsafe_uri"),
        ("file:///tmp/state.cfg.ic", "state_snapshot_index_object_unsupported_uri"),
    ],
)
def test_file_state_snapshot_index_rejects_unsafe_state_object_uris(
    tmp_path: Path,
    state_uri: str,
    expected_reason: str,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"unsafe-uri")
    object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    entry = {
        "state_id": "state_gfs_model_a_2026052106",
        "model_id": "model_a",
        "run_id": "analysis_gfs_2026052018_model_a",
        "source_id": "gfs",
        "valid_time": "2026-05-21T06:00:00Z",
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "lead_hours": 12,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": "package-sha",
    }
    _write_state_index_payload(index_path, [entry], generated_at="2026-05-21T12:00:00Z")
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is False
    assert evidence["reason"] == expected_reason
    assert "candidate_state" not in evidence
    assert str(object_root) not in json.dumps(evidence, sort_keys=True)


def test_file_state_snapshot_index_accepts_relative_object_key_compatibility(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    content = _valid_ic_bytes(b"relative-key")
    object_store.write_bytes_atomic("states/gfs/model_a/2026052106/state.cfg.ic", content)
    index_path = tmp_path / "state-index.json"
    entry = {
        "state_id": "state_gfs_model_a_2026052106",
        "model_id": "model_a",
        "run_id": "analysis_gfs_2026052018_model_a",
        "source_id": "gfs",
        "valid_time": "2026-05-21T06:00:00Z",
        "state_uri": "states/gfs/model_a/2026052106/state.cfg.ic",
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "lead_hours": 12,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": "package-sha",
    }
    publish_state_snapshot_index(
        [entry],
        index_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert evidence["ready"] is True
    assert evidence["candidate_state"]["init_state_uri"] == "states/gfs/model_a/2026052106/state.cfg.ic"


def test_strict_warm_start_evidence_uses_one_index_snapshot(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    first_content = _valid_ic_bytes(b"first-snapshot")
    second_content = _valid_ic_bytes(b"second-snapshot")
    first_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/first.cfg.ic", first_content)
    second_uri = object_store.write_bytes_atomic("states/gfs/model_a/2026052106/second.cfg.ic", second_content)
    first_path = tmp_path / "first-index.json"
    second_path = tmp_path / "second-index.json"
    first_entry = _state_index_test_entry(first_uri, first_content, state_id="state_first")
    second_entry = _state_index_test_entry(second_uri, second_content, state_id="state_second")
    publish_state_snapshot_index(
        [first_entry],
        first_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    publish_state_snapshot_index(
        [second_entry],
        second_path,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        generated_at=_dt("2026-05-21T12:00:00Z"),
    )
    payloads = [
        (json.loads(first_path.read_text(encoding="utf-8")), first_path.read_bytes()),
        (json.loads(second_path.read_text(encoding="utf-8")), second_path.read_bytes()),
    ]
    reads = {"count": 0}

    def flipping_read_payload(
        self: FileStateSnapshotIndexRepository,
        *,
        allow_empty: bool,
    ) -> tuple[dict[str, Any], bytes]:
        del self, allow_empty
        index = min(reads["count"], len(payloads) - 1)
        reads["count"] += 1
        payload, content = payloads[index]
        return dict(payload), bytes(content)

    monkeypatch.setattr(FileStateSnapshotIndexRepository, "_read_payload", flipping_read_payload)
    repository = FileStateSnapshotIndexRepository(
        str(first_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=_dt("2026-05-21T12:00:00Z"),
    )

    evidence = repository.strict_warm_start_evidence(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T06:00:00Z"),
        model_package_version="s3://nhms/models/model_a/package/",
        model_package_checksum="package-sha",
    )

    assert reads["count"] == 1
    assert evidence["ready"] is True
    assert evidence["candidate_state"]["init_state_id"] == "state_first"
    assert evidence["state_snapshot_index"]["object_evidence"]["checksum_verified"] is True


def test_file_state_snapshot_index_concurrent_upserts_preserve_distinct_entries(tmp_path: Path) -> None:
    object_root = tmp_path / "objects"
    object_store = LocalObjectStore(object_root, "s3://nhms")
    index_path = tmp_path / "state-index.json"
    generated_at = _dt("2026-05-21T12:00:00Z")
    snapshots: list[StateSnapshot] = []
    for source_id, hour in (("gfs", "06"), ("ifs", "12")):
        content = _valid_ic_bytes(f"{source_id}-state".encode("utf-8"))
        state_uri = object_store.write_bytes_atomic(
            f"states/{source_id}/model_a/20260521{hour}/state.cfg.ic",
            content,
        )
        snapshots.append(
            StateSnapshot(
                state_id=f"state_{source_id}_model_a_20260521{hour}",
                model_id="model_a",
                run_id=f"analysis_{source_id}_20260521{hour}_model_a",
                valid_time=_dt(f"2026-05-21T{hour}:00:00Z"),
                state_uri=state_uri,
                checksum=f"sha256:{sha256_bytes(content)}",
                usable_flag=True,
                source_id=source_id,
                cycle_id=f"{source_id}_20260521{hour}",
                lead_hours=12,
                model_package_version="s3://nhms/models/model_a/package/",
                model_package_checksum="package-sha",
            )
        )
    barrier = threading.Barrier(len(snapshots))

    def upsert(snapshot: StateSnapshot) -> None:
        repository = FileStateSnapshotIndexRepository(
            str(index_path),
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
            now=generated_at,
            create_missing=True,
        )
        barrier.wait(timeout=5)
        repository.upsert_state_snapshot(snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        for result in [executor.submit(upsert, snapshot) for snapshot in snapshots]:
            result.result(timeout=10)

    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        now=generated_at,
    )
    listed = repository.list_state_snapshots(model_id="model_a", usable=True, limit=10, offset=0)

    assert listed["total_count"] == 2
    assert {item["state_id"] for item in listed["items"]} == {snapshot.state_id for snapshot in snapshots}


def test_db_free_state_save_qc_writes_file_index_without_db_factories(monkeypatch: Any, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    object_root = tmp_path / "objects"
    index_path = object_root / "scheduler" / "state-index.json"
    index_path.parent.mkdir(parents=True)
    run_id = "fcst_gfs_2026052106_model_a"
    output_dir = workspace / "runs" / run_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "model_a.cfg.ic.update").write_text(
        _valid_ic_bytes(b"state-index-save").decode("utf-8"),
        encoding="utf-8",
    )
    manifest_index = tmp_path / "manifest-index.json"
    manifest_index.write_text(
        json.dumps(
            [
                {
                    "task_id": 0,
                    "run_id": run_id,
                    "model_id": "model_a",
                    "basin_version_id": "basin_a_v1",
                    "river_network_version_id": "basin_a_rivnet_v1",
                    "source_id": "gfs",
                    "cycle_time": "2026-05-21T06:00:00Z",
                    "end_time": "2026-05-21T18:00:00Z",
                    "workspace_dir": str(workspace),
                    "model_package_uri": "s3://nhms/models/model_a/package/",
                    "model_package_checksum": "package-sha",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", "file")
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX", str(index_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "packages.common.state_cli.StateRunRepository.from_env",
        lambda: pytest.fail("DB-free state save must not load run context from PostgreSQL"),
    )
    monkeypatch.setattr(
        "packages.common.state_cli.PsycopgStateSnapshotRepository.from_env",
        lambda: pytest.fail("DB-free state save must not construct PsycopgStateSnapshotRepository"),
    )

    result_code = _state_cli_exit_code(["save", "--manifest-index", str(manifest_index), "--task-id", "0"])
    repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    snapshot = repository.get_state_snapshot_by_model_time(
        model_id="model_a",
        source_id="gfs",
        valid_time=_dt("2026-05-21T18:00:00Z"),
    )

    assert result_code == 0
    assert snapshot is not None
    stored_state = LocalObjectStore(object_root, "s3://nhms").read_bytes(snapshot.state_uri)
    assert snapshot.checksum == sha256_bytes(stored_state)
    assert snapshot.usable_flag is True
    assert snapshot.source_id == "gfs"
    assert snapshot.cycle_id == "gfs_2026052106"
    assert snapshot.valid_time == _dt("2026-05-21T18:00:00Z")
    assert snapshot.lead_hours == 12
    assert snapshot.model_package_version == "s3://nhms/models/model_a/package/"
    assert snapshot.model_package_checksum == "package-sha"
    assert snapshot.original_shud_filename == "model_a.cfg.ic.update"


def test_save_state_snapshot_rejects_oversized_input_before_upload(
    monkeypatch: Any,
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    monkeypatch.setattr(state_manager_module, "MAX_STATE_IC_BYTES", 8)
    oversized_ic = tmp_path / "oversized.cfg.ic"
    oversized_ic.write_bytes(b"123456789")

    with pytest.raises(StateManagerError, match="exceeds size limit"):
        manager.save_state_snapshot(
            model_id="demo_model",
            run_id="run_oversized",
            valid_time=_dt("2026-04-30T00:00:00Z"),
            ic_file_path=oversized_ic,
        )

    assert repository.snapshots == {}


def test_save_state_snapshot_rejects_symlink_input_before_upload(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    target = tmp_path / "real.cfg.ic"
    target.write_bytes(_valid_ic_bytes(b"real-symlink-target"))
    symlink = tmp_path / "linked.cfg.ic"
    symlink.symlink_to(target)

    with pytest.raises(StateManagerError, match="Failed to read state snapshot file"):
        manager.save_state_snapshot(
            model_id="demo_model",
            run_id="run_symlink",
            valid_time=_dt("2026-04-30T00:00:00Z"),
            ic_file_path=symlink,
        )

    assert repository.snapshots == {}


def test_state_save_checkpoint_ic_read_is_bounded_before_normalization(
    monkeypatch: Any,
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    monkeypatch.setattr(state_cli, "MAX_STATE_IC_BYTES", 8)
    run_id = "fcst_gfs_2026052106_model_a"
    workspace = tmp_path / "workspace"
    output_dir = workspace / "runs" / run_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "model_a.cfg.ic.update").write_bytes(b"123456789")
    run = state_cli.StateRunContext(
        run_id=run_id,
        model_id="model_a",
        end_time=_dt("2026-05-21T18:00:00Z"),
        output_uri=None,
        source_id="gfs",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
    )

    with pytest.raises(StateManagerError, match="state checkpoint IC file exceeds size limit"):
        state_cli.save_state_for_run(run_id, manager=manager, run_context=run, workspace_root=workspace)

    assert repository.snapshots == {}


def test_state_checkpoint_manifest_rejects_symlink_checkpoint_ic(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    run_id = "fcst_gfs_2026052106_model_a"
    workspace = tmp_path / "workspace"
    output_dir = workspace / "runs" / run_id / "output"
    manifest_dir = output_dir / "state_checkpoints"
    manifest_dir.mkdir(parents=True)
    target = manifest_dir / "real.cfg.ic.update"
    target.write_bytes(_valid_ic_bytes(b"manifest-symlink-target"))
    symlink = manifest_dir / "linked.cfg.ic.update"
    symlink.symlink_to(target)
    (manifest_dir / "state_checkpoints.json").write_text(
        json.dumps(
            {
                "checkpoints": [
                    {
                        "relative_path": "state_checkpoints/linked.cfg.ic.update",
                        "valid_time": "2026-05-21T18:00:00Z",
                        "checkpoint_filename": "linked.cfg.ic.update",
                        "lead_hours": 12,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    run = state_cli.StateRunContext(
        run_id=run_id,
        model_id="model_a",
        end_time=_dt("2026-05-21T18:00:00Z"),
        output_uri=None,
        source_id="gfs",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
    )

    with pytest.raises(StateManagerError, match="State checkpoint path is unsafe"):
        state_cli.save_state_for_run(run_id, manager=manager, run_context=run, workspace_root=workspace)

    assert repository.snapshots == {}


@pytest.mark.parametrize("case_name", ["oversized", "symlink"])
def test_state_checkpoint_manifest_read_is_bounded_no_follow(
    monkeypatch: Any,
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
    case_name: str,
) -> None:
    run_id = "fcst_gfs_2026052106_model_a"
    workspace = tmp_path / "workspace"
    manifest_dir = workspace / "runs" / run_id / "output" / "state_checkpoints"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "state_checkpoints.json"
    if case_name == "oversized":
        monkeypatch.setattr(state_cli, "MAX_STATE_CHECKPOINT_MANIFEST_BYTES", 8)
        manifest_path.write_text('{"checkpoints": []}', encoding="utf-8")
    else:
        target = tmp_path / "linked-state-checkpoints.json"
        target.write_text('{"checkpoints": []}', encoding="utf-8")
        manifest_path.symlink_to(target)
    run = state_cli.StateRunContext(
        run_id=run_id,
        model_id="model_a",
        end_time=_dt("2026-05-21T18:00:00Z"),
        output_uri=None,
        source_id="gfs",
        cycle_time=_dt("2026-05-21T06:00:00Z"),
    )

    with pytest.raises(StateManagerError, match="Invalid state checkpoint manifest"):
        state_cli.save_state_for_run(run_id, manager=manager, run_context=run, workspace_root=workspace)

    assert repository.snapshots == {}


def test_oversized_state_object_fails_qc_without_crash(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    # An oversized stored IC object must fail QC with STATE_FILE_TOO_LARGE (bounded
    # read / OOM protection), not be read unboundedly into memory.
    result = _save_ic(tmp_path, manager, content=b"valid-state")
    snapshot = repository.snapshots[result.state_id]
    # Overwrite the stored object with content exceeding the read limit.
    oversized = b"1 0.1\n" * ((MAX_STATE_IC_BYTES // 6) + 16)
    manager.object_store.write_bytes_atomic(snapshot.state_uri, oversized)
    # Keep the recorded checksum consistent so the size guard (not checksum) trips.
    repository.snapshots[result.state_id] = replace(snapshot, checksum=sha256_bytes(oversized))

    passed = manager.run_qc(result.state_id)

    assert passed is False
    assert repository.snapshots[result.state_id].usable_flag is False
    assert repository.qc_results[-1]["checks_json"]["error_code"] == "STATE_FILE_TOO_LARGE"


def test_latest_usable_state_selects_max_valid_time(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    older = _save_ic(tmp_path, manager, valid_time=_dt("2026-04-28T00:00:00Z"), content=b"older")
    latest = _save_ic(tmp_path, manager, valid_time=_dt("2026-04-29T00:00:00Z"), content=b"latest")
    future = _save_ic(tmp_path, manager, valid_time=_dt("2026-05-01T00:00:00Z"), content=b"future")
    repository.set_usable_flag(state_id=older.state_id, usable_flag=True)
    repository.set_usable_flag(state_id=latest.state_id, usable_flag=True)
    repository.set_usable_flag(state_id=future.state_id, usable_flag=True)

    selected = manager.get_latest_usable_state(
        model_id="demo_model",
        before_time=_dt("2026-04-30T00:00:00Z"),
    )

    assert selected is not None
    assert selected.state_id == latest.state_id


def test_assess_freshness_boundaries() -> None:
    cycle_time = _dt("2026-05-08T00:00:00Z")

    assert assess_freshness(None, cycle_time) == "cold_start_no_state"
    assert assess_freshness(cycle_time - timedelta(days=7), cycle_time) == "fresh"
    assert assess_freshness(cycle_time - timedelta(days=7, seconds=1), cycle_time) == "degraded_stale_init_state"
    assert assess_freshness(cycle_time - timedelta(days=30), cycle_time) == "degraded_stale_init_state"
    assert assess_freshness(cycle_time - timedelta(days=30, seconds=1), cycle_time) == "cold_start_stale_state"


def test_conflicting_checksum_overwrites_snapshot_as_superseded(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    first = _save_ic(tmp_path, manager, content=b"first")
    repository.set_usable_flag(state_id=first.state_id, usable_flag=True)

    second_path = tmp_path / "second.cfg.ic"
    second_path.write_bytes(_valid_ic_bytes(b"second"))
    second = manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_002",
        valid_time=_dt("2026-04-30T00:00:00Z"),
        ic_file_path=second_path,
    )

    assert second.status == "superseded"
    assert second.state_id == first.state_id
    snapshot = repository.snapshots[second.state_id]
    assert snapshot.run_id == "run_002"
    assert snapshot.checksum == sha256_bytes(_valid_ic_bytes(b"second"))
    assert snapshot.usable_flag is False


def test_source_specific_snapshots_do_not_supersede_each_other(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    gfs_path = tmp_path / "gfs.cfg.ic"
    ifs_path = tmp_path / "ifs.cfg.ic"
    gfs_path.write_bytes(_valid_ic_bytes(b"gfs"))
    ifs_path.write_bytes(_valid_ic_bytes(b"ifs"))
    valid_time = _dt("2026-04-30T00:00:00Z")

    gfs = manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_gfs",
        valid_time=valid_time,
        ic_file_path=gfs_path,
        source_id="gfs",
    )
    ifs = manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_ifs",
        valid_time=valid_time,
        ic_file_path=ifs_path,
        source_id="IFS",
    )

    assert gfs.status == "created"
    assert ifs.status == "created"
    assert gfs.state_id == "state_gfs_demo_model_2026043000"
    assert ifs.state_id == "state_IFS_demo_model_2026043000"
    assert len(repository.snapshots) == 2
    assert repository.get_state_snapshot_by_model_time(
        model_id="demo_model", valid_time=valid_time, source_id="gfs"
    ).state_id == gfs.state_id
    assert repository.get_state_snapshot_by_model_time(
        model_id="demo_model", valid_time=valid_time, source_id="IFS"
    ).state_id == ifs.state_id
    assert gfs.snapshot.state_uri == "states/gfs/demo_model/2026043000/state.cfg.ic"
    assert ifs.snapshot.state_uri == "states/IFS/demo_model/2026043000/state.cfg.ic"


def test_same_checksum_save_is_idempotent(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    first = _save_ic(tmp_path, manager, content=b"same")
    second_path = tmp_path / "same-again.cfg.ic"
    second_path.write_bytes(_valid_ic_bytes(b"same"))

    second = manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_001",
        valid_time=_dt("2026-04-30T00:00:00Z"),
        ic_file_path=second_path,
    )

    assert second.status == "already_done"
    assert second.state_id == first.state_id
    assert len(repository.snapshots) == 1


@pytest.mark.asyncio
async def test_state_snapshot_api_list_and_get(
    tmp_path: Path,
    manager: StateManager,
    repository: FakeStateSnapshotRepository,
) -> None:
    older = _save_ic(tmp_path, manager, valid_time=_dt("2026-04-28T00:00:00Z"), content=b"older")
    latest = _save_ic(tmp_path, manager, valid_time=_dt("2026-04-29T00:00:00Z"), content=b"latest")
    repository.set_usable_flag(state_id=older.state_id, usable_flag=True)
    repository.set_usable_flag(state_id=latest.state_id, usable_flag=True)
    app.dependency_overrides[get_state_manager] = lambda: manager

    list_response = await _get("/api/v1/state-snapshots?model_id=demo_model&usable=true")
    get_response = await _get(f"/api/v1/state-snapshots/{latest.state_id}")

    assert list_response.status_code == 200
    list_data = list_response.json()
    assert list_data["total_count"] == 2
    assert [item["state_id"] for item in list_data["items"]] == [latest.state_id, older.state_id]
    assert get_response.status_code == 200
    assert get_response.json()["state_id"] == latest.state_id


def _valid_ic_bytes(content: bytes) -> bytes:
    # Structurally-valid SHUD .cfg.ic body; vary the minute-time token by content so
    # distinct callers keep distinct checksums while passing state-variable QC.
    minute = 27_000_000.0 + (int.from_bytes(content[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [
        f"2\t1\t{minute:.6f}",
        "1\t0.1\t0.1\t0.1\t0.1\t0.1",
        "2\t0.1\t0.1\t0.1\t0.1\t0.1",
        "1\t0.5",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_state_index_payload(path: Path, entries: list[dict[str, Any]], *, generated_at: str) -> None:
    payload: dict[str, Any] = {
        "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
        "generated_at": generated_at,
        "entries": entries,
    }
    payload["checksum"] = f"sha256:{_payload_checksum(payload)}"
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    content = json.dumps(
        {key: value for key, value in payload.items() if key != "checksum"},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256_bytes(content)


def _state_index_test_entry(state_uri: str, content: bytes, *, state_id: str) -> dict[str, Any]:
    return {
        "state_id": state_id,
        "model_id": "model_a",
        "run_id": "analysis_gfs_2026052018_model_a",
        "source_id": "gfs",
        "valid_time": "2026-05-21T06:00:00Z",
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "cycle_id": "gfs_2026052018",
        "lead_hours": 12,
        "model_package_version": "s3://nhms/models/model_a/package/",
        "model_package_checksum": "package-sha",
    }


def _state_cli_exit_code(argv: list[str]) -> int:
    try:
        return state_cli.main(argv)
    except SystemExit as error:
        return int(error.code or 0)


def _save_ic(
    tmp_path: Path,
    manager: StateManager,
    *,
    valid_time: datetime | None = None,
    content: bytes,
) -> Any:
    valid_time = valid_time or _dt("2026-04-30T00:00:00Z")
    path = tmp_path / f"{content.decode('utf-8')}.cfg.ic"
    path.write_bytes(_valid_ic_bytes(content))
    return manager.save_state_snapshot(
        model_id="demo_model",
        run_id="run_001",
        valid_time=valid_time,
        ic_file_path=path,
    )


async def _get(path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _snapshot_dict(snapshot: StateSnapshot) -> dict[str, Any]:
    return {
        "state_id": snapshot.state_id,
        "model_id": snapshot.model_id,
        "run_id": snapshot.run_id,
        "valid_time": _format_time(snapshot.valid_time),
        "state_uri": snapshot.state_uri,
        "checksum": snapshot.checksum,
        "usable_flag": snapshot.usable_flag,
        "created_at": _format_time(snapshot.created_at),
    }


def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        candidate = value
    else:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _dt(value).isoformat().replace("+00:00", "Z")
