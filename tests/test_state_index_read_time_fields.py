"""#3123: no state-index publish persists the read-time injected fields.

Validation injects ``index_generated_at`` and ``object_evidence`` into every
entry on read.  ``upsert_state_snapshot`` and ``set_usable_flag`` used to load
entries that way and publish them back, so the private lane carried two extra
JSON nodes per entry (node-22, 2026-11-11: 252,205 vs 227,897 nodes for 12,154
entries per lane).

Seams: ``FileStateSnapshotIndexRepository.upsert_state_snapshot`` /
``set_usable_flag`` / ``validated_entries_for_renewal``,
``publish_state_snapshot_index``, ``merge_state_snapshot_index_copyback`` and
the read validator ``_validate_state_snapshot_index`` (the only seam that
returns entries with the injected fields).  Every index under test is seeded as
raw bytes with an independently computed checksum, never through the publisher
under test, and expected values come from the seeded entries and an
independent JSON node counter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common import state_manager as state_manager_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import (
    FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
    FileStateSnapshotIndexRepository,
    StateSnapshot,
    merge_state_snapshot_index_copyback,
    publish_state_snapshot_index,
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination

PREFIX = "s3://nhms"
NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
SEEDED_AT = "2026-11-11T06:00:00Z"
INDEX_KEY = "scheduler/state-index/index-last.json"
# Literal on purpose: the contract is these two names, not whatever the module calls them.
READ_TIME_FIELDS = ("index_generated_at", "object_evidence")
# Every key `_normalize_state_index_entry` emits, i.e. the copyback (raw) shape.
NORMALIZED_KEYS = (
    "state_id",
    "model_id",
    "run_id",
    "source_id",
    "valid_time",
    "state_uri",
    "checksum",
    "usable_flag",
    "created_at",
    "cycle_id",
    "lead_hours",
    "model_package_version",
    "model_package_checksum",
    "original_shud_filename",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_state_bytes(seed: int) -> bytes:
    minute = 27_000_000.0 + seed
    return (f"2\t1\t{minute:.6f}\n1\t0.1\t0.1\t0.1\t0.1\t0.1\n2\t0.1\t0.1\t0.1\t0.1\t0.1\n1\t0.5\n").encode()


def _independent_checksum(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "checksum"}
    content = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{sha256_bytes(content)}"


def _count_nodes(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count_nodes(child) for child in value.values())
    if isinstance(value, list):
        return 1 + sum(_count_nodes(child) for child in value)
    return 1


def _object_root(tmp_path: Path, name: str = "object-store") -> Path:
    root = (tmp_path / name).resolve()
    make_directory_with_explicit_mode(root / "scheduler" / "state-index")
    return root


def _full_entry(object_root: Path, index: int) -> dict[str, Any]:
    """An entry carrying every normalized key, backed by a real state object."""

    model_id = f"basins_m{index}_shud"
    valid_time = NOW - timedelta(hours=12 * (index + 1))
    cycle_id = f"gfs_{(valid_time - timedelta(hours=12)):%Y%m%d%H}"
    content = _valid_state_bytes(index)
    state_uri = LocalObjectStore(object_root, PREFIX).write_bytes_atomic(
        f"states/gfs/{model_id}/{valid_time:%Y%m%d%H}/state.cfg.ic", content
    )
    entry = {
        "state_id": f"state_gfs_{model_id}_{valid_time:%Y%m%d%H}_{cycle_id}_f012",
        "model_id": model_id,
        "run_id": f"fcst_{cycle_id}_{model_id}",
        "source_id": "gfs",
        "valid_time": _iso(valid_time),
        "state_uri": state_uri,
        "checksum": f"sha256:{sha256_bytes(content)}",
        "usable_flag": True,
        "created_at": _iso(valid_time),
        "cycle_id": cycle_id,
        "lead_hours": 12,
        "model_package_version": f"{PREFIX}/models/{model_id}/package/",
        "model_package_checksum": "f" * 64,
        "original_shud_filename": f"{model_id}.f012.cfg.ic.update",
    }
    assert tuple(entry) == NORMALIZED_KEYS
    return entry


def _legacy(entry: dict[str, Any]) -> dict[str, Any]:
    """The pre-#3123 private-lane shape: both read-time fields persisted (object_evidence null).

    The persisted ``index_generated_at`` is an older publish's header time, so a read that
    re-injects it from the current header is observable.
    """

    return {**entry, "index_generated_at": "2026-11-10T18:00:00Z", "object_evidence": None}


def _write_raw_index(index_path: Path, entries: list[dict[str, Any]]) -> None:
    payload: dict[str, Any] = {
        "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
        "generated_at": SEEDED_AT,
        "entries": entries,
    }
    payload["checksum"] = _independent_checksum(payload)
    write_provider_destination(index_path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    assert _disk_entries(index_path) == entries


def _disk_payload(index_path: Path) -> dict[str, Any]:
    return json.loads(index_path.read_text(encoding="utf-8"))


def _disk_entries(index_path: Path) -> list[dict[str, Any]]:
    return _disk_payload(index_path)["entries"]


def _assert_clean(index_path: Path) -> None:
    payload = _disk_payload(index_path)
    assert payload["checksum"] == _independent_checksum(payload)
    for entry in payload["entries"]:
        assert not set(READ_TIME_FIELDS) & set(entry), entry


def _repository(object_root: Path) -> FileStateSnapshotIndexRepository:
    return FileStateSnapshotIndexRepository(
        str(object_root / INDEX_KEY),
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        now=NOW,
        create_missing=True,
    )


def _new_snapshot(object_root: Path) -> StateSnapshot:
    content = _valid_state_bytes(900)
    state_uri = LocalObjectStore(object_root, PREFIX).write_bytes_atomic(
        "states/gfs/basins_new_shud/2026111112/state.cfg.ic", content
    )
    return StateSnapshot(
        state_id="state_gfs_basins_new_shud_2026111112_gfs_2026111100_f012",
        model_id="basins_new_shud",
        run_id="fcst_gfs_2026111100_basins_new_shud",
        valid_time=NOW,
        state_uri=state_uri,
        checksum=sha256_bytes(content),
        usable_flag=False,
        source_id="gfs",
        cycle_id="gfs_2026111100",
        lead_hours=12,
    )


def _renew(object_root: Path) -> None:
    """The scheduler file-provider refresh renewal: read for renewal, republish under the preimage."""

    entries, _evidence, preimage = _repository(object_root).validated_entries_for_renewal()
    publish_state_snapshot_index(
        entries,
        object_root / INDEX_KEY,
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        generated_at=NOW,
        expected_preimage=preimage,
    )


def test_upsert_does_not_persist_read_time_fields_on_existing_entries(tmp_path: Path) -> None:
    object_root = _object_root(tmp_path)
    seeded = [_full_entry(object_root, index) for index in range(3)]
    _write_raw_index(object_root / INDEX_KEY, seeded)
    snapshot = _new_snapshot(object_root)

    _repository(object_root).upsert_state_snapshot(snapshot)

    _assert_clean(object_root / INDEX_KEY)
    by_id = {entry["state_id"]: entry for entry in _disk_entries(object_root / INDEX_KEY)}
    # The reloaded existing entries are the ones the old code re-persisted with both fields.
    assert [by_id[entry["state_id"]] for entry in seeded] == seeded
    assert by_id[snapshot.state_id]["usable_flag"] is False
    assert len(by_id) == 4


def test_set_usable_flag_does_not_persist_read_time_fields(tmp_path: Path) -> None:
    object_root = _object_root(tmp_path)
    seeded = [_full_entry(object_root, index) for index in range(3)]
    _write_raw_index(object_root / INDEX_KEY, seeded)

    updated = _repository(object_root).set_usable_flag(state_id=seeded[1]["state_id"], usable_flag=False)

    assert updated is not None and updated.usable_flag is False
    _assert_clean(object_root / INDEX_KEY)
    expected = [dict(entry) for entry in seeded]
    expected[1]["usable_flag"] = False
    assert sorted(_disk_entries(object_root / INDEX_KEY), key=lambda entry: entry["state_id"]) == sorted(
        expected, key=lambda entry: entry["state_id"]
    )


def test_publish_drops_read_time_fields_and_reads_back_with_them_injected(tmp_path: Path) -> None:
    object_root = _object_root(tmp_path)
    clean = _full_entry(object_root, 0)
    carrying = {
        **clean,
        "index_generated_at": "2026-11-10T00:00:00Z",
        "object_evidence": {"checksum_verified": True, "provider": "stale"},
    }
    caller_copy = dict(carrying)

    publish_state_snapshot_index(
        [carrying],
        object_root / INDEX_KEY,
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        generated_at=NOW,
    )

    assert carrying == caller_copy, "the writer must not mutate the caller's entries"
    _assert_clean(object_root / INDEX_KEY)
    payload = _disk_payload(object_root / INDEX_KEY)
    assert payload["entries"] == [clean]
    # Reading it back: the checksum validates and both fields are injected in memory only.
    validated = state_manager_module._validate_state_snapshot_index(
        payload,
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        published_artifact_root=None,
        now=NOW,
        max_age_hours=168,
        verify_objects=True,
    )
    (entry,) = validated.values()
    assert entry["index_generated_at"] == _iso(NOW)
    assert entry["object_evidence"]["checksum_verified"] is True
    evidence = _repository(object_root).state_index_evidence()
    assert evidence["status"] == "ready"
    assert evidence["content_checksum_verified"] is True


@pytest.mark.parametrize("publish_path", ["set_usable_flag", "upsert", "provider_refresh_renewal"])
def test_legacy_index_carrying_the_fields_reads_and_is_clean_after_one_publish(
    tmp_path: Path, publish_path: str
) -> None:
    object_root = _object_root(tmp_path)
    seeded = [_full_entry(object_root, index) for index in range(3)]
    _write_raw_index(object_root / INDEX_KEY, [_legacy(entry) for entry in seeded])
    assert all(set(READ_TIME_FIELDS) <= set(entry) for entry in _disk_entries(object_root / INDEX_KEY))

    # The read succeeds with both fields present in memory, overwritten from this read
    # (the disk carried an older header time and a null object_evidence).
    in_memory = state_manager_module._validate_state_snapshot_index(
        _disk_payload(object_root / INDEX_KEY),
        object_store_root=object_root,
        object_store_prefix=PREFIX,
        published_artifact_root=None,
        now=NOW,
        max_age_hours=168,
        verify_objects=True,
    )
    assert len(in_memory) == len(seeded)
    for entry in in_memory.values():
        assert entry["index_generated_at"] == SEEDED_AT
        assert entry["object_evidence"]["checksum_verified"] is True
    repository = _repository(object_root)
    evidence = repository.state_index_evidence()
    assert evidence["status"] == "ready"
    assert evidence["content_checksum_verified"] is True
    snapshot = repository.get_state_snapshot(seeded[0]["state_id"])
    assert snapshot is not None and snapshot.checksum == seeded[0]["checksum"]

    if publish_path == "set_usable_flag":
        repository.set_usable_flag(state_id=seeded[0]["state_id"], usable_flag=True)
    elif publish_path == "upsert":
        repository.upsert_state_snapshot(_new_snapshot(object_root))
    else:
        _renew(object_root)

    _assert_clean(object_root / INDEX_KEY)
    by_id = {entry["state_id"]: entry for entry in _disk_entries(object_root / INDEX_KEY)}
    assert [by_id[entry["state_id"]] for entry in seeded] == seeded


def test_set_usable_flag_json_nodes_equal_the_copyback_shape(tmp_path: Path) -> None:
    private_root = _object_root(tmp_path, "object-store")
    shared_root = _object_root(tmp_path, "shared-object-store")
    seeded = [_full_entry(private_root, index) for index in range(3)]
    _write_raw_index(private_root / INDEX_KEY, seeded)

    _repository(private_root).set_usable_flag(state_id=seeded[2]["state_id"], usable_flag=False)
    merge_state_snapshot_index_copyback(
        source_path=private_root / INDEX_KEY,
        destination_path=shared_root / INDEX_KEY,
        reference_object_store_root=private_root,
        object_store_prefix=PREFIX,
        source_containment_root=private_root,
        destination_containment_root=shared_root,
    )

    private_nodes = _count_nodes(_disk_payload(private_root / INDEX_KEY))
    shared_nodes = _count_nodes(_disk_payload(shared_root / INDEX_KEY))
    # Root object + schema_version + generated_at + checksum + entries list, then
    # one object node plus one scalar per normalized key for every entry.
    expected = 5 + len(seeded) * (1 + len(NORMALIZED_KEYS))
    assert (private_nodes, shared_nodes) == (expected, expected)
