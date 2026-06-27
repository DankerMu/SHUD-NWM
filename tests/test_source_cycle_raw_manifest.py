from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.source_cycle_raw_manifest import (
    NfsRawManifestStagingError,
    forecast_cycle_from_raw_manifest_readiness,
    nfs_raw_manifest_readiness,
    stage_nfs_raw_manifest_to_object_store,
)


def _write_manifest(
    root: Path,
    *,
    source_id: str = "gfs",
    cycle: str = "2026062612",
    entries: list[dict[str, Any]] | None = None,
    manifest_uri: str | None = None,
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
                "metadata": {"physical_file_count": len({entry["local_key"] for entry in entries})},
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


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
    assert staged["source_object_store_root"] == "[local-path]"
    assert staged["target_object_store_root"] == "[local-path]"
    assert (target_root / "raw/gfs/2026062612/file-a.grib2").read_bytes() == b"grib-bytes"
    assert json.loads((target_root / "raw/gfs/2026062612/manifest.json").read_text(encoding="utf-8"))[
        "source_id"
    ] == "gfs"


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
