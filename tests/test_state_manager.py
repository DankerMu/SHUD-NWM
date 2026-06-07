from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.state_snapshots import get_state_manager
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    StateManager,
    StateSnapshot,
    assess_freshness,
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
