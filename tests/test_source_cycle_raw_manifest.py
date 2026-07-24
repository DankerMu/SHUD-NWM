from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.source_cycle_raw_manifest import (
    NfsRawManifestStagingError,
    forecast_cycle_from_raw_manifest_readiness,
    nfs_raw_manifest_readiness,
    stage_nfs_raw_manifest_from_env,
    stage_nfs_raw_manifest_to_object_store,
)


def _write_manifest(
    root: Path,
    *,
    source_id: str = "gfs",
    cycle: str = "2026062612",
    entries: list[dict[str, Any]] | None = None,
    manifest_uri: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    entries = entries or [
        {
            "remote_url": "https://example.invalid/gfs",
            "local_key": f"raw/{source_id}/{cycle}/file-a.grib2",
            "variable": "prcp_rate_or_amount",
            "forecast_hour": 0,
        }
    ]
    for local_key in {str(entry["local_key"]) for entry in entries}:
        path = root / local_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"grib-bytes")
    manifest_path = root / f"raw/{source_id}/{cycle}/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "source_id": source_id,
                "cycle_time": "2026-06-26T12:00:00+00:00",
                "manifest_uri": manifest_uri or f"s3://nhms/raw/{source_id}/{cycle}/manifest.json",
                "metadata": metadata or {"physical_file_count": len({entry["local_key"] for entry in entries})},
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def _unexpected_destination_lock(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("invalid raw manifest identity must fail before destination lock")


def test_nfs_raw_manifest_readiness_accepts_complete_gfs_manifest(tmp_path: Path) -> None:
    _write_manifest(tmp_path)

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        object_store_prefix="s3://nhms",
        required=True,
    )

    assert readiness["status"] == "ready"
    assert readiness["required"] is True
    assert readiness["manifest_uri"] == "s3://nhms/raw/gfs/2026062612/manifest.json"
    assert readiness["entry_count"] == 1
    assert readiness["physical_file_count"] == 1
    assert readiness["total_bytes"] > 0

    forecast_cycle = forecast_cycle_from_raw_manifest_readiness(
        readiness,
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
    )
    assert forecast_cycle["status"] == "raw_complete"
    assert forecast_cycle["manifest_uri"] == readiness["manifest_uri"]
    assert forecast_cycle["source_cycle_truth"] == "node27_nfs_raw_manifest"


def test_nfs_raw_manifest_readiness_accepts_canonical_prefix_with_trailing_separator(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path)

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        object_store_prefix="s3://nhms/",
        required=True,
    )

    assert readiness["status"] == "ready"
    assert readiness["manifest_uri"] == "s3://nhms/raw/gfs/2026062612/manifest.json"


@pytest.mark.parametrize(
    "manifest_uri",
    [
        "s3://other/raw/gfs/2026062612/manifest.json",
        "s3://nhms/alternate/raw/gfs/2026062612/manifest.json",
        "",
    ],
    ids=("other-authority", "nested-prefix", "missing-uri"),
)
def test_nfs_raw_manifest_readiness_requires_exact_configured_manifest_uri(
    tmp_path: Path,
    manifest_uri: str,
) -> None:
    _write_manifest(tmp_path)
    manifest_path = tmp_path / "raw/gfs/2026062612/manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_uri"] = manifest_uri
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        object_store_prefix="s3://nhms",
        required=True,
    )

    assert readiness["status"] == "invalid"
    assert readiness["reason"] == "manifest_uri_mismatch"
    assert readiness["manifest_key"] == "raw/gfs/2026062612/manifest.json"


def test_source_object_identity_from_raw_manifest_readiness_uses_manifest_metadata(tmp_path: Path) -> None:
    source_object_identity = {
        "source": "gfs",
        "manifest_object_key": "raw/gfs/2026062612/manifest.json",
        "manifest_digest": "persisted-digest",
        "raw_entry_digest": "raw-entry-digest",
    }
    source_policy = {
        "source": "gfs",
        "policy_schema_version": "nhms.gfs.source_policy.v3",
        "cycle_hours_utc": [0, 6, 12, 18],
    }
    _write_manifest(
        tmp_path,
        metadata={
            "physical_file_count": 1,
            "source_object_identity": source_object_identity,
            "source_policy": source_policy,
        },
    )
    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        object_store_prefix="s3://nhms",
        required=True,
    )

    from services.orchestrator.source_cycle_raw_manifest import (
        source_object_identity_from_raw_manifest_readiness,
        source_policy_from_raw_manifest_readiness,
    )

    assert source_object_identity_from_raw_manifest_readiness(readiness) == source_object_identity
    assert source_policy_from_raw_manifest_readiness(readiness) == source_policy


def test_nfs_raw_manifest_readiness_accepts_ifs_uppercase_storage(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        source_id="IFS",
        manifest_uri="s3://nhms/raw/IFS/2026062612/manifest.json",
    )

    readiness = nfs_raw_manifest_readiness(
        source_id="ifs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
    )

    assert readiness["status"] == "ready"
    assert readiness["source_id"] == "IFS"
    assert readiness["manifest_key"] == "raw/IFS/2026062612/manifest.json"


@pytest.mark.parametrize(
    "local_key",
    [
        "raw/ifs/2026062612/foreign-source.grib2",
        "raw/gfs/2026062600/foreign-cycle.grib2",
    ],
    ids=("wrong-source", "wrong-cycle"),
)
def test_nfs_raw_manifest_readiness_rejects_entry_outside_requested_source_cycle(
    tmp_path: Path,
    local_key: str,
) -> None:
    _write_manifest(tmp_path, entries=[{"local_key": local_key}])

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        required=True,
    )

    assert (tmp_path / local_key).read_bytes() == b"grib-bytes"
    assert readiness["status"] == "invalid"
    assert readiness["reason"] == "manifest_entry_local_key_identity_mismatch"
    assert readiness["entry_index"] == 0
    assert readiness["expected_source_id"] == "gfs"
    assert readiness["expected_cycle"] == "2026062612"


@pytest.mark.parametrize(
    "local_key",
    [
        "raw/ifs/2026062612/foreign-source.grib2",
        "raw/gfs/2026062600/foreign-cycle.grib2",
    ],
    ids=("wrong-source", "wrong-cycle"),
)
@pytest.mark.parametrize("same_root", [False, True], ids=("distinct-root", "same-root"))
def test_stage_nfs_raw_manifest_revalidates_entry_source_cycle_from_forged_readiness(
    tmp_path: Path,
    local_key: str,
    same_root: bool,
) -> None:
    source_root = tmp_path / "nfs"
    _write_manifest(source_root, entries=[{"local_key": local_key}])
    forged_readiness = {
        "status": "ready",
        "required": True,
        "source": "node27_nfs_raw_manifest",
        "source_id": "gfs",
        "cycle_id": "gfs_2026062612",
        "cycle_time": "2026-06-26T12:00:00Z",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "manifest_key": "raw/gfs/2026062612/manifest.json",
        "manifest_path": str(source_root / "raw/gfs/2026062612/manifest.json"),
        "object_store_root": str(source_root),
    }

    with pytest.raises(
        NfsRawManifestStagingError,
        match="^manifest_entry_local_key_identity_mismatch$",
    ):
        stage_nfs_raw_manifest_to_object_store(
            forged_readiness,
            target_object_store_root=(
                source_root if same_root else tmp_path / "scratch-object-store"
            ),
        )

    if not same_root:
        assert not (tmp_path / "scratch-object-store/raw/gfs/2026062612/manifest.json").exists()


@pytest.mark.parametrize(
    "manifest_key",
    [
        "raw/ifs/2026062612/manifest.json",
        "raw/gfs/2026062600/manifest.json",
        "raw/gfs/2026062612/not-manifest.json",
    ],
    ids=("wrong-source", "wrong-cycle", "wrong-filename"),
)
@pytest.mark.parametrize("same_root", [False, True], ids=("distinct-root", "same-root"))
def test_stage_nfs_raw_manifest_rejects_manifest_key_outside_exact_source_cycle_before_mutation(
    tmp_path: Path,
    manifest_key: str,
    same_root: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import source_cycle_raw_manifest as raw_manifest_module

    monkeypatch.setattr(raw_manifest_module, "provider_destination_lock", _unexpected_destination_lock)
    source_root = tmp_path / "nfs"
    target_root = source_root if same_root else tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    canonical_manifest = source_root / "raw/gfs/2026062612/manifest.json"
    before = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    forged_readiness = {
        "status": "ready",
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00Z",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "manifest_key": manifest_key,
        "manifest_path": str(canonical_manifest),
        "object_store_root": str(source_root),
    }

    with pytest.raises(NfsRawManifestStagingError, match="^manifest_key_identity_mismatch$"):
        stage_nfs_raw_manifest_to_object_store(
            forged_readiness,
            target_object_store_root=target_root,
        )

    after = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    if not same_root:
        assert not target_root.exists()


@pytest.mark.parametrize("same_root", [False, True], ids=("distinct-root", "same-root"))
def test_stage_nfs_raw_manifest_rejects_mismatched_manifest_path_before_mutation(
    tmp_path: Path,
    same_root: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import source_cycle_raw_manifest as raw_manifest_module

    monkeypatch.setattr(raw_manifest_module, "provider_destination_lock", _unexpected_destination_lock)
    source_root = tmp_path / "nfs"
    target_root = source_root if same_root else tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    canonical_manifest = source_root / "raw/gfs/2026062612/manifest.json"
    alternate_manifest = canonical_manifest.with_name("alternate-manifest.json")
    alternate_manifest.write_bytes(canonical_manifest.read_bytes())
    before = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    forged_readiness = {
        "status": "ready",
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00Z",
        "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
        "manifest_key": "raw/gfs/2026062612/manifest.json",
        "manifest_path": str(alternate_manifest),
        "object_store_root": str(source_root),
    }

    with pytest.raises(NfsRawManifestStagingError, match="^manifest_path_identity_mismatch$"):
        stage_nfs_raw_manifest_to_object_store(
            forged_readiness,
            target_object_store_root=target_root,
        )

    after = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    if not same_root:
        assert not target_root.exists()


@pytest.mark.parametrize("same_root", [False, True], ids=("distinct-root", "same-root"))
def test_stage_nfs_raw_manifest_rejects_cross_prefix_payload_before_mutation(
    tmp_path: Path,
    same_root: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.orchestrator import source_cycle_raw_manifest as raw_manifest_module

    monkeypatch.setattr(raw_manifest_module, "provider_destination_lock", _unexpected_destination_lock)
    source_root = tmp_path / "nfs"
    target_root = source_root if same_root else tmp_path / "scratch-object-store"
    _write_manifest(
        source_root,
        manifest_uri="s3://other/raw/gfs/2026062612/manifest.json",
    )
    canonical_manifest = source_root / "raw/gfs/2026062612/manifest.json"
    before = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    forged_readiness = {
        "status": "ready",
        "source_id": "gfs",
        "cycle_time": "2026-06-26T12:00:00Z",
        "manifest_uri": "s3://other/raw/gfs/2026062612/manifest.json",
        "manifest_key": "raw/gfs/2026062612/manifest.json",
        "manifest_path": str(canonical_manifest),
        "object_store_root": str(source_root),
    }

    with pytest.raises(NfsRawManifestStagingError, match="^manifest_uri_mismatch$"):
        stage_nfs_raw_manifest_to_object_store(
            forged_readiness,
            target_object_store_root=target_root,
            source_object_store_prefix="s3://nhms",
        )

    after = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    if not same_root:
        assert not target_root.exists()


def test_stage_nfs_raw_manifest_from_env_reconstructs_trusted_manifest_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=source_root,
        object_store_prefix="s3://nhms",
        required=True,
    )
    public_readiness = {**readiness, "manifest_path": "[local-path]"}
    monkeypatch.setenv("NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(source_root))
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_PREFIX", "s3://nhms")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_STAGE_ROOT", str(target_root))

    staged = stage_nfs_raw_manifest_from_env({"nfs_raw_manifest": public_readiness})

    assert staged is not None
    assert staged["status"] == "staged"
    assert (target_root / "raw/gfs/2026062612/manifest.json").is_file()


def test_nfs_raw_manifest_readiness_rejects_manifest_before_files_land(tmp_path: Path) -> None:
    entries = [
        {
            "remote_url": "https://example.invalid/gfs",
            "local_key": "raw/gfs/2026062612/missing.grib2",
            "variable": "prcp_rate_or_amount",
            "forecast_hour": 0,
        }
    ]
    manifest_path = tmp_path / "raw/gfs/2026062612/manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "source_id": "gfs",
                "cycle_time": "2026-06-26T12:00:00+00:00",
                "manifest_uri": "s3://nhms/raw/gfs/2026062612/manifest.json",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=tmp_path,
        required=True,
    )

    assert readiness["status"] == "invalid"
    assert readiness["reason"] == "raw_files_missing"
    assert readiness["required"] is True


def test_stage_nfs_raw_manifest_to_compute_visible_object_store(tmp_path: Path) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    stale_manifest = target_root / "raw/gfs/2026062612/manifest.json"
    stale_manifest.parent.mkdir(parents=True, exist_ok=True)
    stale_manifest.write_text("stale\n", encoding="utf-8")

    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=source_root,
        object_store_prefix="s3://nhms",
        required=True,
    )

    staged = stage_nfs_raw_manifest_to_object_store(
        readiness,
        target_object_store_root=target_root,
        target_object_store_prefix="s3://nhms",
    )

    assert staged["status"] == "staged"
    assert staged["staged_file_count"] == 1
    assert staged["manifest_uri"] == "[object-uri]"
    assert staged["source_object_store_root"] == "[local-path]"
    assert staged["target_object_store_root"] == "[local-path]"
    assert (target_root / "raw/gfs/2026062612/file-a.grib2").read_bytes() == b"grib-bytes"
    assert json.loads((target_root / "raw/gfs/2026062612/manifest.json").read_text(encoding="utf-8"))[
        "source_id"
    ] == "gfs"


def test_stage_nfs_raw_manifest_preserves_normalized_ifs_storage_variant(tmp_path: Path) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(
        source_root,
        source_id="IFS",
        manifest_uri="s3://nhms/raw/IFS/2026062612/manifest.json",
    )
    readiness = nfs_raw_manifest_readiness(
        source_id="ifs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=source_root,
        object_store_prefix="s3://nhms",
        required=True,
    )

    staged = stage_nfs_raw_manifest_to_object_store(
        readiness,
        target_object_store_root=target_root,
        source_object_store_prefix="s3://nhms",
        target_object_store_prefix="s3://nhms",
    )

    assert staged["status"] == "staged"
    assert (target_root / "raw/IFS/2026062612/manifest.json").is_file()


def test_stage_nfs_raw_manifest_serializes_same_source_cycle_and_reuses_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=source_root,
        object_store_prefix="s3://nhms",
        required=True,
    )
    from services.orchestrator import source_cycle_raw_manifest as raw_manifest_module

    original_copy = raw_manifest_module._copy_object_file
    copied_keys: list[str] = []

    def counted_copy(source: Path, target: Path, key: str) -> int:
        copied_keys.append(key)
        return original_copy(source, target, key)

    monkeypatch.setattr(raw_manifest_module, "_copy_object_file", counted_copy)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _index: stage_nfs_raw_manifest_to_object_store(
                    readiness,
                    target_object_store_root=target_root,
                    target_object_store_prefix="s3://nhms",
                ),
                range(8),
            )
        )

    assert [result["status"] for result in results].count("staged") == 1
    assert [result["status"] for result in results].count("skipped") == 7
    assert sorted(copied_keys) == [
        "raw/gfs/2026062612/file-a.grib2",
        "raw/gfs/2026062612/manifest.json",
    ]

    repeated = stage_nfs_raw_manifest_to_object_store(
        readiness,
        target_object_store_root=target_root,
        target_object_store_prefix="s3://nhms",
    )
    assert repeated["status"] == "skipped"
    assert repeated["reason"] == "already_staged"
    assert len(copied_keys) == 2


def test_stage_nfs_raw_manifest_replaces_changed_source_generation(tmp_path: Path) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(source_root, metadata={"generation": 1})
    cycle_time = datetime(2026, 6, 26, 12, tzinfo=UTC)
    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        object_store_root=source_root,
        required=True,
    )
    first = stage_nfs_raw_manifest_to_object_store(
        readiness,
        target_object_store_root=target_root,
    )
    assert first["status"] == "staged"

    _write_manifest(source_root, metadata={"generation": 2})
    (source_root / "raw/gfs/2026062612/file-a.grib2").write_bytes(b"new-grib-generation")
    refreshed = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=cycle_time,
        object_store_root=source_root,
        required=True,
    )
    second = stage_nfs_raw_manifest_to_object_store(
        refreshed,
        target_object_store_root=target_root,
    )

    assert second["status"] == "staged"
    assert (target_root / "raw/gfs/2026062612/file-a.grib2").read_bytes() == b"new-grib-generation"
    assert json.loads((target_root / "raw/gfs/2026062612/manifest.json").read_text(encoding="utf-8"))[
        "metadata"
    ] == {"generation": 2}


def test_stage_nfs_raw_manifest_does_not_publish_manifest_when_raw_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "nfs"
    target_root = tmp_path / "scratch-object-store"
    _write_manifest(source_root)
    readiness = nfs_raw_manifest_readiness(
        source_id="gfs",
        cycle_time=datetime(2026, 6, 26, 12, tzinfo=UTC),
        object_store_root=source_root,
        object_store_prefix="s3://nhms",
        required=True,
    )

    def fail_copyfile(src: Path, dst: Path, *, follow_symlinks: bool = True) -> None:
        del dst, follow_symlinks
        if str(src).endswith("file-a.grib2"):
            raise OSError("raw copy failed")

    monkeypatch.setattr("services.orchestrator.source_cycle_raw_manifest.shutil.copyfile", fail_copyfile)

    with pytest.raises(NfsRawManifestStagingError):
        stage_nfs_raw_manifest_to_object_store(
            readiness,
            target_object_store_root=target_root,
            target_object_store_prefix="s3://nhms",
        )

    assert not (target_root / "raw/gfs/2026062612/manifest.json").exists()
