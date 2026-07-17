from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    provider_destination_lock,
    read_provider_snapshot,
)
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator import scheduler_file_providers as scheduler_file_providers_module
from services.orchestrator.scheduler_file_providers import (
    FileCanonicalReadinessProvider,
    SchedulerFileProviderError,
    capture_scheduler_provider_preimage,
    derive_catalog_bound_readiness_entries,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
    validate_catalog_bound_readiness_entries,
    validate_readiness_registry_model_set,
)
from workers.canonical_converter.converter import required_standard_variables_for_source


def _config(tmp_path: Path) -> refresh.RefreshConfig:
    basins = tmp_path / "Basins"
    objects = tmp_path / "objects"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, objects, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (work, receipts, emergency, tmp_path / "private"):
        path.chmod(0o700)
    scheduler = objects / "scheduler"
    (scheduler / "registry").mkdir(parents=True)
    (scheduler / "canonical-readiness").mkdir()
    (scheduler / "state-index").mkdir()
    return refresh.RefreshConfig(
        basins_root=basins,
        registry_uri=str(scheduler / "registry" / "manifest-last.json"),
        readiness_uri=str(scheduler / "canonical-readiness" / "index-last.json"),
        state_uri=str(scheduler / "state-index" / "index-last.json"),
        object_store_root=objects,
        provider_store_root=objects,
        object_store_prefix="s3://nhms",
        workspace_root=work,
        receipt_root=receipts,
        emergency_root=emergency,
        refresh_lock=tmp_path / "private" / "refresh",
    )


def _write_canonical_catalog(
    object_root: Path,
    *,
    source_id: str,
    cycle: str,
    policy_identity: dict[str, object] | None = None,
    source_object_identity: dict[str, object] | None = None,
) -> tuple[str, dict[str, object], dict[str, object]]:
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    policy = policy_identity or {"source": source_id, "cycle": cycle}
    source_object = source_object_identity or {
        "source": source_id,
        "manifest_object_key": f"raw/{source_id}/{cycle}/manifest.json",
    }
    products = []
    for variable in required_standard_variables_for_source(source_id):
        key = f"canonical/{source_id}/{cycle}/{variable}/f003.dat"
        content = f"{source_id}:{cycle}:{variable}:3".encode()
        store.write_bytes_atomic(key, content)
        products.append(
            {
                "canonical_product_id": f"{source_id}_{cycle}_{variable}_f003",
                "source_id": source_id,
                "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
                "valid_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T03:00:00Z",
                "lead_time_hours": 3,
                "variable": variable,
                "object_uri": store.uri_for_key(key),
                "checksum": f"sha256:{sha256_bytes(content)}",
                "quality_flag": "ok",
                "lineage_json": {
                    "policy_identity": policy,
                    "source_object_identity": source_object,
                },
            }
        )
    catalog_key = f"canonical/{source_id}/{cycle}/_catalog/catalog.json"
    content = json.dumps(
        {
            "schema_version": "nhms.canonical.product_catalog.v1",
            "source_id": source_id,
            "cycle_time": f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:]}:00:00Z",
            "products": products,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return store.write_bytes_atomic(catalog_key, content), policy, source_object


def test_catalog_derivation_builds_two_sources_for_exact_registry_model_set(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [
        {"model_id": f"model-{index:02d}", "basin_id": f"basin-{index:02d}"}
        for index in range(13)
    ]

    entries, evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    validation = validate_catalog_bound_readiness_entries(
        entries,
        models,
        destination_uri=tmp_path / "shared/scheduler/canonical-readiness/index-last.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )

    assert len(entries) == 26
    assert evidence["model_set"]["source_entry_counts"] == {"gfs": 13, "IFS": 13}
    assert validation["model_set"]["status"] == "matched"
    assert all(entry["products"] == [] for entry in entries)
    assert all(entry["catalog_uri"].startswith("s3://nhms/canonical/") for entry in entries)
    assert all(entry["catalog_sha256"].startswith("sha256:") for entry in entries)
    assert all(entry["catalog_row_count"] > 0 for entry in entries)


def test_catalog_bound_consumer_recomputes_identity_and_detects_catalog_mutation(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    catalog_uri, policy, source_object = _write_canonical_catalog(
        object_root,
        source_id="gfs",
        cycle="2026071400",
    )
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    entries, _evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    for readiness_entry in entries:
        if readiness_entry["source_id"] == "gfs":
            readiness_entry["policy_identity"] = {"source": "gfs", "cycle": "stale"}
            readiness_entry["source_object_identity"] = {"manifest_object_key": "raw/gfs/stale/manifest.json"}
    destination = tmp_path / "shared/scheduler/canonical-readiness/index-last.json"
    publish_canonical_readiness_index(
        entries,
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        verify_external_references=True,
    )
    provider = FileCanonicalReadinessProvider(
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    entry = next(item for item in entries if item["source_id"] == "gfs")
    recomputed = provider.canonical_readiness(
        source_id="gfs",
        cycle_time=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        forecast_hours=(3,),
        policy_identity=policy,
        source_object_identity=source_object,
        canonical_product_id=str(entry["canonical_product_id"]),
        model_id="model-a",
        basin_id="basin-a",
    )
    assert recomputed["ready"] is True
    assert recomputed["readiness_index"]["entry_status"] == "identity_mismatch_recomputed"

    catalog_path = LocalObjectStore(object_root, "s3://nhms").resolve_path(catalog_uri)
    catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")
    mutated_provider = FileCanonicalReadinessProvider(
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    blocked = mutated_provider.canonical_readiness(
        source_id="gfs",
        cycle_time=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        forecast_hours=(3,),
        policy_identity=policy,
        source_object_identity=source_object,
        canonical_product_id=str(entry["canonical_product_id"]),
        model_id="model-a",
        basin_id="basin-a",
    )
    assert blocked["ready"] is False
    assert blocked["reason"] == "canonical_readiness_index_identity_mismatch"
    assert blocked["readiness_index"]["catalog"]["reason"] == "readiness_catalog_checksum_mismatch"


def test_catalog_derivation_fails_closed_on_invalid_newest_cycle(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071300")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    newest = object_root / "canonical/gfs/2026071400/_catalog"
    newest.mkdir(parents=True)
    (newest / "catalog.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(SchedulerFileProviderError) as error_info:
        derive_catalog_bound_readiness_entries(
            [{"model_id": "model-a", "basin_id": "basin-a"}],
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "file_manifest_malformed_json"


def test_precommit_validation_rejects_catalog_changed_after_derivation(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    catalog_uri, _policy, _source_object = _write_canonical_catalog(
        object_root,
        source_id="gfs",
        cycle="2026071400",
    )
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    entries, _evidence = derive_catalog_bound_readiness_entries(
        models,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )
    store = LocalObjectStore(object_root, "s3://nhms")
    catalog_path = store.resolve_path(catalog_uri)
    catalog_path.write_bytes(catalog_path.read_bytes() + b"\n")

    with pytest.raises(SchedulerFileProviderError) as error_info:
        validate_catalog_bound_readiness_entries(
            entries,
            models,
            destination_uri=tmp_path / "shared/scheduler/canonical-readiness/index-last.json",
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_catalog_checksum_mismatch"


def test_catalog_derivation_rejects_symlink_and_bounded_cycle_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    (object_root / "canonical/gfs/unsafe").symlink_to(object_root / "canonical/gfs/2026071400")
    models = [{"model_id": "model-a", "basin_id": "basin-a"}]
    with pytest.raises(SchedulerFileProviderError) as symlink_error:
        derive_catalog_bound_readiness_entries(
            models,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )
    assert symlink_error.value.reason == "canonical_catalog_scan_unsafe_entry"

    (object_root / "canonical/gfs/unsafe").unlink()
    monkeypatch.setattr(scheduler_file_providers_module, "MAX_CANONICAL_CATALOG_CYCLE_DIRS", 1)
    (object_root / "canonical/gfs/grid").mkdir()
    with pytest.raises(SchedulerFileProviderError) as limit_error:
        derive_catalog_bound_readiness_entries(
            models,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )
    assert limit_error.value.reason == "canonical_catalog_cycle_limit_exceeded"


def test_catalog_derivation_streams_objects_under_hard_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "private-objects"
    _write_canonical_catalog(object_root, source_id="gfs", cycle="2026071400")
    _write_canonical_catalog(object_root, source_id="IFS", cycle="2026071400")
    monkeypatch.setattr(scheduler_file_providers_module, "MAX_CANONICAL_PRODUCT_OBJECT_BYTES", 4)

    with pytest.raises(SchedulerFileProviderError) as error_info:
        derive_catalog_bound_readiness_entries(
            [{"model_id": "model-a", "basin_id": "basin-a"}],
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_product_object_size_limit_exceeded"


def test_registry_readiness_cross_check_rejects_missing_model() -> None:
    with pytest.raises(SchedulerFileProviderError) as error_info:
        validate_readiness_registry_model_set(
            [
                {"source_id": source, "model_id": "model-a", "basin_id": "basin-a"}
                for source in ("gfs", "IFS")
            ],
            [
                {"model_id": "model-a", "basin_id": "basin-a"},
                {"model_id": "model-b", "basin_id": "basin-b"},
            ],
        )
    assert error_info.value.reason == "readiness_registry_model_set_mismatch"


def test_legacy_readiness_entries_are_not_renewed(tmp_path: Path) -> None:
    object_root = tmp_path / "private-objects"
    destination = tmp_path / "shared/scheduler/canonical-readiness/index-last.json"
    publish_canonical_readiness_index(
        [
            {
                "source_id": "gfs",
                "cycle_time": "2026-07-14T00:00:00Z",
                "model_id": "model-a",
                "basin_id": "basin-a",
                "canonical_product_id": "canon_gfs_2026071400",
                "forecast_hours": [3],
                "policy_identity": {"source": "gfs"},
                "source_object_identity": {"manifest": "legacy"},
                "products": [],
            }
        ],
        destination,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
    )

    with pytest.raises(SchedulerFileProviderError) as error_info:
        scheduler_file_providers_module.load_canonical_readiness_entries_for_renewal(
            destination,
            object_store_root=object_root,
            object_store_prefix="s3://nhms",
        )

    assert error_info.value.reason == "readiness_catalog_binding_required"


def _preimage(value: str = "old") -> ProviderPreimage:
    return ProviderPreimage(
        exists=True,
        sha256=value * (64 // len(value)) if len(value) < 64 else value[:64],
        device=1,
        inode=2,
        mode=0o600,
        uid=1,
        gid=1,
        size=10,
        mtime_ns=20,
    )


def _minimal_registry_manifest_bytes(name: str) -> bytes:
    """Minimal but shape-valid manifest bytes for gate tests.

    #1080 gate parses the previous canonical manifest; opaque byte fixtures
    (b"registry\n") would fail as ``provider_invalid`` before the classifier
    can run.  Empty ``models`` array covers the "first publication / fresh
    inventory" case so the gate classifies everything as ``added``.
    """
    return json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "models": [],
            "checksum": f"sha256:{'0' * 64}",
            "note": name,
        },
        sort_keys=True,
    ).encode() + b"\n"


def _classification_stub() -> dict[str, object]:
    return {
        "previous_registry_sha256": None,
        "new_registry_sha256": None,
        "added": {"items": [], "total": 0, "truncated": False},
        "unchanged": {"items": [], "total": 0, "truncated": False},
        "removed": {"items": [], "total": 0, "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }


def _write_current_published_receipt(config: refresh.RefreshConfig) -> tuple[Path, dict[str, object]]:
    providers = []
    provider_paths = [("registry", config.registry_uri)]
    if config.worker_registry_uri is not None:
        provider_paths.append(("registry_worker_mirror", config.worker_registry_uri))
    provider_paths.extend((("readiness", config.readiness_uri), ("state", config.state_uri)))
    for name, uri in provider_paths:
        path = Path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        if name in {"registry", "registry_worker_mirror"}:
            # Use shape-valid manifest bytes so the #1080 gate can parse the
            # previous canonical without treating it as provider_invalid.
            path.write_bytes(_minimal_registry_manifest_bytes("registry"))
        else:
            path.write_text(name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = refresh._receipt(
        run_id="refresh_current",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=providers,
        registry_classification=_classification_stub(),
    )
    receipt_path = config.receipt_root / "latest.json"
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    return receipt_path, receipt


def _stub_provider_pipeline(monkeypatch: pytest.MonkeyPatch, *, committed: bool = True) -> None:
    del committed
    preimage = ProviderPreimage(exists=False)

    def capture(uri: str, *args: object, **kwargs: object) -> ProviderPreimage:
        del args, kwargs
        del uri
        return preimage

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(
        refresh,
        "_read_provider_header",
        lambda *args, **kwargs: {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-01T00:00:00Z",
            "checksum": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"entry": "valid"}], {"status": "ready", "entry_count": 26}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [
                {"model_id": f"model-{index}", "basin_id": f"basin-{index}"}
                for index in range(13)
            ],
        )
        return {
            "selected_model_count": 13,
            "registry": None
            if kwargs["dry_run"]
            else {"checksum": "sha256:" + "d" * 64, "model_count": 13},
        }

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "e" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        refresh,
        "publish_state_snapshot_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "f" * 64, "entry_count": 1},
    )


def _seed_empty_provider_files(config: refresh.RefreshConfig) -> None:
    generated = refresh.datetime.now(refresh.UTC)
    publish_scheduler_registry_manifest(
        [],
        config.registry_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )
    publish_canonical_readiness_index(
        [],
        config.readiness_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )
    publish_state_snapshot_index(
        [],
        config.state_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )


def _stub_catalog_bound_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"catalog_bound": True}], {"status": "ready", "entry_count": 1}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})


def test_provider_atomic_expected_preimage_preserves_concurrent_update(tmp_path: Path) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")
    expected = capture_provider_preimage(destination, max_bytes=1024)
    destination.write_bytes(b"authoritative-new")
    before = os.stat(destination)

    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(
            destination,
            b"refresh",
            max_bytes=1024,
            expected_preimage=expected,
        )

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"authoritative-new"
    after = os.stat(destination)
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
    )
    assert after_identity == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
    )


def test_provider_snapshot_rejects_replacement_between_metadata_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"generation-a")
    real_read = provider_atomic_module.read_bytes_limited_no_follow
    replaced = False

    def replace_before_read(*args: object, **kwargs: object) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            destination.write_bytes(b"generation-b")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "read_bytes_limited_no_follow", replace_before_read)
    with pytest.raises(ProviderAtomicError) as error_info:
        read_provider_snapshot(destination, max_bytes=1024)

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"generation-b"


def test_provider_atomic_postread_failure_restores_validated_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")
    expected = capture_provider_preimage(destination, max_bytes=1024)
    real_write = provider_atomic_module.atomic_write_bytes_no_follow
    calls = 0

    def corrupt_first(path: Path, content: bytes, **kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        return real_write(path, b"corrupt" if calls == 1 else content, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", corrupt_first)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024, expected_preimage=expected)

    assert error_info.value.reason == "provider_restored_previous"
    assert destination.read_bytes() == b"old"


def test_provider_atomic_durable_replace_uncertainty_is_not_reported_as_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")

    def uncertain(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise SafeFilesystemError("directory fsync failed", kind="indeterminate")

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", uncertain)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.reason == "provider_replace_uncertain"
    assert error_info.value.phase == "replace_uncertain"


def test_provider_destination_lock_contender_is_nonblocking(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    with provider_destination_lock(destination, blocking=False):
        with pytest.raises(ProviderAtomicError) as error_info:
            with provider_destination_lock(destination, blocking=False):
                pass
    assert error_info.value.reason == "provider_already_running"


def test_provider_destination_lock_still_excludes_cross_process_contender(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    repository = Path(__file__).resolve().parents[1]
    contender = """
import sys
from pathlib import Path
from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock

try:
    with provider_destination_lock(Path(sys.argv[1]), blocking=False):
        print("unexpectedly-acquired")
except ProviderAtomicError as error:
    print(error.reason)
"""

    with provider_destination_lock(destination):
        result = subprocess.run(
            [sys.executable, "-c", contender, str(destination)],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    assert result.stdout.strip() == "provider_already_running"


def test_provider_destination_lock_serializes_threads_when_flock_is_process_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", lambda *_args: None)
    start = threading.Barrier(20)
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum_active = 0
    errors: list[BaseException] = []

    def contender() -> None:
        nonlocal active, maximum_active
        try:
            start.wait()
            with provider_destination_lock(destination):
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active > 1:
                        release_first.set()
                release_first.wait(timeout=0.05)
                with guard:
                    active -= 1
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=contender) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1
    assert not provider_atomic_module._PROCESS_LOCKS


def test_provider_atomic_readers_observe_only_complete_old_or_new_json(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    old = json.dumps({"generation": "old", "rows": list(range(50))}).encode()
    new = json.dumps({"generation": "new", "rows": list(range(100))}).encode()
    destination.write_bytes(old)
    finished = threading.Event()
    observed: list[bytes] = []

    def writer() -> None:
        for index in range(40):
            atomic_replace_provider_bytes(destination, new if index % 2 else old, max_bytes=4096)
        finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    while not finished.is_set():
        observed.append(destination.read_bytes())
    thread.join()

    assert observed
    assert set(observed) <= {old, new}
    assert all(json.loads(content)["generation"] in {"old", "new"} for content in observed)


def test_provider_atomic_publishes_shared_mode_under_private_umask(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    previous_umask = os.umask(0o077)
    try:
        atomic_replace_provider_bytes(destination, b"shared", max_bytes=1024)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.stat().st_uid == os.geteuid()


def test_provider_lock_rejects_writable_parent_and_preserves_body_errors(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(unsafe / "manifest.json"):
            pass
    assert error_info.value.reason == "provider_lock_parent_unsafe"

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with provider_destination_lock(shared / "manifest.json"):
        pass

    with pytest.raises(SafeFilesystemError):
        with provider_destination_lock(tmp_path / "manifest.json"):
            raise SafeFilesystemError("body failure")


def test_provider_lock_revalidates_lock_path_after_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest.json"
    lock_path = provider_atomic_module.provider_lock_path(destination)
    real_flock = provider_atomic_module.fcntl.flock
    swapped = False

    def swap_after_lock(fd: int, flags: int) -> None:
        nonlocal swapped
        real_flock(fd, flags)
        if flags & provider_atomic_module.fcntl.LOCK_EX and not swapped:
            swapped = True
            replacement = tmp_path / "replacement.lock"
            replacement.write_bytes(b"")
            os.replace(replacement, lock_path)

    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", swap_after_lock)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(destination):
            pass

    assert error_info.value.reason == "provider_lock_changed"


def test_provider_postreplace_read_exception_is_not_precommit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    destination.write_bytes(b"old")
    real_capture = provider_atomic_module.capture_provider_preimage
    calls = 0

    def fail_after_replace(*args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise SafeFilesystemError("post-replace read failed")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", fail_after_replace)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.phase == "replace_uncertain"
    assert error_info.value.reason == "provider_postread_failed"


@pytest.mark.parametrize("provider", ["registry", "readiness", "state"])
def test_all_provider_publishers_reject_changed_expected_preimage(tmp_path: Path, provider: str) -> None:
    destination = tmp_path / f"{provider}.json"
    generated = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    if provider == "registry":
        def publisher(expected=None):
            return publish_scheduler_registry_manifest(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    elif provider == "readiness":
        def publisher(expected=None):
            return publish_canonical_readiness_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    else:
        def publisher(expected=None):
            return publish_state_snapshot_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    previous_umask = os.umask(0o077)
    try:
        publisher()
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    expected = capture_scheduler_provider_preimage(destination)
    authoritative = json.loads(destination.read_text())
    authoritative["extra_authoritative_field"] = "new"
    destination.write_text(json.dumps(authoritative))
    preserved = destination.read_bytes()

    with pytest.raises(Exception) as error_info:
        publisher(expected)

    assert getattr(error_info.value, "reason", "") == "provider_preimage_changed"
    assert destination.read_bytes() == preserved


def test_refresh_dry_run_validates_three_providers_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert receipt["database_free"] is True
    assert [provider["name"] for provider in receipt["providers"]] == ["registry", "readiness", "state"]
    assert receipt["providers"][0]["entry_count"] == 13
    assert receipt["providers"][1]["entry_count"] == 26
    assert all(provider["after_sha256"] == provider["before_sha256"] for provider in receipt["providers"])
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt
    assert not list(config.emergency_root.iterdir())


def test_provider_evidence_prefers_entry_count_over_model_count() -> None:
    evidence = refresh._provider_evidence(
        "readiness",
        {},
        {"entry_count": 40, "model_count": 20},
    )

    assert evidence["entry_count"] == 40


def test_refresh_routes_shared_provider_and_private_reference_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _config(tmp_path)
    private_references = tmp_path / "private-reference-objects"
    private_references.mkdir()
    config = replace(original, object_store_root=private_references)
    _stub_provider_pipeline(monkeypatch)
    seen: dict[str, Path] = {}
    preimage = _preimage("a")

    def capture(*args: object, **kwargs: object) -> ProviderPreimage:
        del args
        seen["registry_capture"] = Path(str(kwargs["object_store_root"]))
        return preimage

    def readiness(*args: object, **kwargs: object):
        del args
        seen["readiness"] = Path(str(kwargs["object_store_root"]))
        return ([{"entry": "valid"}], {"status": "ready", "entry_count": 26})

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            seen["state"] = Path(str(kwargs["object_store_root"]))

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    def registry(**kwargs: object) -> dict[str, object]:
        seen["registry_publish"] = Path(str(kwargs["object_store_root"]))
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-1", "basin_id": "basin-1"}],
        )
        return {"selected_model_count": 13, "registry": None, "packages": []}

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(refresh, "derive_catalog_bound_readiness_entries", readiness)
    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", registry)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert seen["registry_capture"] == config.provider_store_root
    assert seen["registry_publish"] == config.object_store_root
    assert seen["readiness"] == config.object_store_root
    assert seen["state"] == config.object_store_root


def test_refresh_commits_identical_worker_registry_generation_and_binds_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert [provider["name"] for provider in receipt["providers"]] == [
        "registry",
        "registry_worker_mirror",
        "readiness",
        "state",
    ]
    assert receipt["providers"][0]["after_sha256"] == receipt["providers"][1]["after_sha256"]
    assert receipt["providers"][0]["entry_count"] == receipt["providers"][1]["entry_count"] == 13
    assert paths["registry"].read_bytes() == paths["registry_worker_mirror"].read_bytes()


def test_refresh_dry_run_rejects_existing_worker_registry_generation_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    shared = Path(config.registry_uri)
    worker = Path(config.worker_registry_uri)
    # Both files must be shape-valid so the #1080 gate does not refuse
    # earlier than the shared/mirror generation-mismatch check.
    shared.write_bytes(_minimal_registry_manifest_bytes("shared"))
    worker.parent.mkdir(parents=True)
    worker.write_bytes(_minimal_registry_manifest_bytes("worker-old"))
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture_scheduler_provider_preimage)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_invalid"


def test_worker_registry_restore_uses_committed_preimage_and_restores_exact_bytes(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/registry/manifest-last.json"),
    )
    worker = Path(config.worker_registry_uri)
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_bytes(b"old-generation")
    before = capture_scheduler_provider_preimage(worker)
    committed = atomic_replace_provider_bytes(
        worker,
        b"prospective-generation",
        containment_root=config.object_store_root,
        max_bytes=refresh.MAX_REGISTRY_MANIFEST_BYTES,
        expected_preimage=before,
    )

    refresh._restore_worker_registry_mirror(config, previous=b"old-generation", expected_current=committed)

    assert worker.read_bytes() == b"old-generation"


def test_refresh_rolls_back_worker_mirror_when_shared_registry_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="registry")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert {name: path.read_bytes() for name, path in paths.items()} == old


def _tracked_transaction_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_lane: str,
    conflict_lane: str = "",
    unowned_lane: str = "",
) -> tuple[refresh.RefreshConfig, dict[str, bytes], dict[str, Path]]:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    paths = {
        "registry": Path(config.registry_uri),
        "registry_worker_mirror": Path(config.worker_registry_uri),
        "readiness": Path(config.readiness_uri),
        "state": Path(config.state_uri),
    }
    # #1080 gate parses previous canonical registry bytes as JSON with a
    # models list; use shape-valid manifest content for the registry lanes.
    _valid_registry = _minimal_registry_manifest_bytes("previous")
    old = {
        "registry": _valid_registry,
        "registry_worker_mirror": _valid_registry,
        "readiness": b"old-readiness-generation",
        "state": b"old-state-generation",
    }
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(old[name])
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture_scheduler_provider_preimage)

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return (
                [{"entry": "valid"}],
                {"checksum": "sha256:" + "c" * 64},
                capture_scheduler_provider_preimage(paths["state"]),
            )

    def replace_bytes(
        name: str,
        content: bytes,
        expected: ProviderPreimage,
        commit_observer: object,
    ) -> dict[str, object]:
        if conflict_lane == name:
            atomic_replace_provider_bytes(
                paths[name],
                f"concurrent-authoritative-{name}".encode(),
                containment_root=(
                    config.object_store_root
                    if name == "registry_worker_mirror"
                    else config.provider_store_root
                ),
                max_bytes=refresh.MAX_READINESS_INDEX_BYTES,
                expected_preimage=expected,
            )
            raise ProviderAtomicError("provider_preimage_changed", phase="precommit")
        committed = atomic_replace_provider_bytes(
            paths[name],
            content,
            containment_root=(
                config.object_store_root
                if name == "registry_worker_mirror"
                else config.provider_store_root
            ),
            max_bytes=refresh.MAX_READINESS_INDEX_BYTES,
            expected_preimage=expected,
        )
        result = {
            "content_sha256": committed.sha256,
            "checksum": "sha256:" + "1" * 64,
            "generated_at": "2026-07-14T02:00:00Z",
            "model_count": 13,
        }
        if name in {"readiness", "state"}:
            result["entry_count"] = 1
        if unowned_lane != name:
            assert callable(commit_observer)
            commit_observer(committed)
        if fail_lane == name:
            raise refresh.RefreshError("provider_invalid", phase="postcommit")
        if unowned_lane == name:
            raise refresh.RefreshError("provider_invalid", phase="postcommit")
        return result

    def publish_mirror(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "registry_worker_mirror",
            b"new-registry-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        evidence = replace_bytes(
            "registry",
            b"new-registry-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("registry_commit_observer"),
        )
        return {
            "selected_model_count": 13,
            "registry": evidence,
            "packages": [],
        }

    def publish_readiness(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "readiness",
            b"new-readiness-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    def publish_state(*args: object, **kwargs: object) -> dict[str, object]:
        del args
        return replace_bytes(
            "state",
            b"new-state-generation",
            ProviderPreimage.from_value(kwargs["expected_preimage"]),
            kwargs.get("commit_observer"),
        )

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(refresh, "publish_scheduler_registry_manifest", publish_mirror)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(refresh, "publish_canonical_readiness_index", publish_readiness)
    monkeypatch.setattr(refresh, "publish_state_snapshot_index", publish_state)
    return config, old, paths


def test_readiness_failure_rolls_back_registry_mirror_and_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="readiness")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert receipt["providers"] == []
    assert {name: path.read_bytes() for name, path in paths.items()} == old
    assert not list(config.emergency_root.iterdir())


def test_state_failure_rolls_back_all_four_provider_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="state")
    original_restore = refresh._restore_provider_path
    rollback_order: list[Path] = []

    def record_restore(path: Path, **kwargs: object) -> None:
        rollback_order.append(path)
        original_restore(path, **kwargs)

    monkeypatch.setattr(refresh, "_restore_provider_path", record_restore)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert receipt["providers"] == []
    assert {name: path.read_bytes() for name, path in paths.items()} == old
    assert rollback_order == [
        paths["state"],
        paths["readiness"],
        paths["registry"],
        paths["registry_worker_mirror"],
    ]


def test_rollback_cas_conflict_is_uncertain_and_never_relabelled_as_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="state")
    original_restore = refresh._restore_provider_path

    def conflict_readiness(path: Path, **kwargs: object) -> None:
        if path == paths["readiness"]:
            path.write_bytes(b"concurrent-authoritative-readiness")
        original_restore(path, **kwargs)

    monkeypatch.setattr(refresh, "_restore_provider_path", conflict_readiness)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    assert receipt["reason"] == "provider_replace_uncertain"
    assert paths["registry"].read_bytes() == old["registry"]
    assert paths["registry_worker_mirror"].read_bytes() == old["registry_worker_mirror"]
    assert paths["state"].read_bytes() == old["state"]
    assert paths["readiness"].read_bytes() == b"concurrent-authoritative-readiness"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert json.loads(emergency[0].read_text())["outcome"] == "replace_uncertain"


@pytest.mark.parametrize(
    "lane",
    ["registry_worker_mirror", "registry", "readiness", "state"],
)
def test_typed_preimage_conflict_preserves_authoritative_lane_and_restores_earlier_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    config, old, paths = _tracked_transaction_fixture(
        tmp_path,
        monkeypatch,
        fail_lane="",
        conflict_lane=lane,
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_preimage_changed"
    assert receipt["providers"] == []
    assert paths[lane].read_bytes() == f"concurrent-authoritative-{lane}".encode()
    assert {name: path.read_bytes() for name, path in paths.items() if name != lane} == {
        name: content for name, content in old.items() if name != lane
    }


def test_generic_write_after_exception_without_commit_token_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, old, paths = _tracked_transaction_fixture(
        tmp_path,
        monkeypatch,
        fail_lane="",
        unowned_lane="state",
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    assert receipt["reason"] == "provider_replace_uncertain"
    assert paths["state"].read_bytes() == b"new-state-generation"
    assert paths["registry"].read_bytes() == old["registry"]
    assert paths["registry_worker_mirror"].read_bytes() == old["registry_worker_mirror"]
    assert paths["readiness"].read_bytes() == old["readiness"]


@pytest.mark.parametrize("lane", ["readiness", "state"])
def test_full_refresh_preserves_newer_authoritative_provider_on_snapshot_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    config = _config(tmp_path)
    _seed_empty_provider_files(config)
    _stub_catalog_bound_derivation(monkeypatch)
    authoritative: dict[str, bytes] = {}

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda _entries, destination, **kwargs: publish_canonical_readiness_index([], destination, **kwargs),
    )
    if lane == "readiness":
        def derive_then_replace(*args: object, **kwargs: object):
            del args, kwargs
            publish_canonical_readiness_index(
                [],
                config.readiness_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                generated_at=refresh.datetime.now(refresh.UTC) + timedelta(seconds=1),
            )
            authoritative["bytes"] = Path(config.readiness_uri).read_bytes()
            return ([{"catalog_bound": True}], {"status": "ready", "entry_count": 1})

        monkeypatch.setattr(refresh, "derive_catalog_bound_readiness_entries", derive_then_replace)
        destination = Path(config.readiness_uri)
    else:
        repository_type = refresh.FileStateSnapshotIndexRepository

        class ReplaceAfterStateSnapshot(repository_type):
            def validated_entries_for_renewal(self):
                snapshot = super().validated_entries_for_renewal()
                publish_state_snapshot_index(
                    [],
                    config.state_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    generated_at=refresh.datetime.now(refresh.UTC) + timedelta(seconds=1),
                )
                authoritative["bytes"] = Path(config.state_uri).read_bytes()
                return snapshot

        monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", ReplaceAfterStateSnapshot)
        destination = Path(config.state_uri)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_preimage_changed"
    assert destination.read_bytes() == authoritative["bytes"]


def test_full_refresh_maps_public_postread_failure_to_restored_previous_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _seed_empty_provider_files(config)
    _stub_catalog_bound_derivation(monkeypatch)
    readiness_before = Path(config.readiness_uri).read_bytes()

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda _entries, destination, **kwargs: publish_canonical_readiness_index([], destination, **kwargs),
    )
    real_capture = provider_atomic_module.capture_provider_preimage
    readiness_capture_count = 0

    def fail_readiness_postread(path: Path, *args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal readiness_capture_count
        if Path(path) == Path(config.readiness_uri):
            readiness_capture_count += 1
            if readiness_capture_count == 2:
                raise SafeFilesystemError("injected readiness post-read failure")
        return real_capture(path, *args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", fail_readiness_postread)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous", receipt
    assert receipt["reason"] == "provider_postread_failed"
    assert receipt["phase"] == "postcommit"
    assert Path(config.readiness_uri).read_bytes() == readiness_before


def test_refresh_publishes_three_provider_digests_and_keeps_32_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    history = config.receipt_root / "history"
    history.mkdir()
    for index in range(35):
        (history / f"old-{index:02d}.json").write_text("{}")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert [item["after_sha256"] for item in receipt["providers"]] == ["d" * 64, "e" * 64, "f" * 64]
    assert len(list(history.iterdir())) == 32


def test_primary_receipt_failure_after_commit_finalizes_mode_0600_emergency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published_receipt_failed"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert os.stat(emergency[0]).st_mode & 0o777 == 0o600
    emergency_receipt = json.loads(emergency[0].read_text())
    assert refresh._validate_receipt(emergency_receipt) == emergency_receipt
    assert emergency_receipt["providers"][2]["after_sha256"] == "f" * 64


def test_primary_and_emergency_receipt_failure_is_replace_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    def fail_emergency(slot: refresh.EmergencySlot, receipt: object) -> None:
        del receipt
        os.close(slot.file_fd)
        os.close(slot.parent_fd)
        raise OSError

    monkeypatch.setattr(refresh, "_finalize_emergency_slot", fail_emergency)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"


def test_emergency_reconstruction_validates_committed_digests_without_republish(tmp_path: Path) -> None:
    config = _config(tmp_path)
    providers = []
    for name, uri in (
        ("registry", config.registry_uri),
        ("readiness", config.readiness_uri),
        ("state", config.state_uri),
    ):
        path = Path(uri)
        path.write_text(name)
        preimage = capture_scheduler_provider_preimage(uri)
        providers.append(
            {
                "name": name,
                "before_sha256": None,
                "before_inode": None,
                "before_schema_version": None,
                "before_generated_at": None,
                "before_payload_checksum": None,
                "after_sha256": preimage.sha256,
                "after_schema_version": None,
                "after_generated_at": None,
                "after_payload_checksum": None,
                "entry_count": 1,
            }
        )
    receipt = refresh._receipt(
        run_id="refresh_reconstruct",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=providers,
    )
    emergency = config.emergency_root / "refresh_reconstruct.reserved.json"
    emergency.write_bytes(refresh._receipt_bytes(receipt))
    emergency.chmod(0o600)

    reconstructed = refresh.reconstruct_primary_receipt(config, emergency)

    assert reconstructed == receipt
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt


def test_emergency_reconstruction_rejects_noncanonical_receipt_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    receipt = refresh._receipt(
        run_id="refresh_invalid_recovery",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=[],
    )
    emergency = config.emergency_root / "refresh_invalid_recovery.reserved.json"
    emergency.write_text(json.dumps({**receipt, "unexpected": "smuggled"}))
    published = False

    def record_publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        del args, kwargs
        published = True

    monkeypatch.setattr(refresh, "_publish_primary_receipt", record_publish)
    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.reconstruct_primary_receipt(config, emergency)

    assert error_info.value.reason == "emergency_record_invalid"
    assert not published


def test_current_receipt_validation_rejects_untrusted_or_stale_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    receipt_path, receipt = _write_current_published_receipt(config)
    assert refresh.validate_current_receipt(config, receipt_path) == receipt

    receipt_path.write_text('{"outcome":"published","database_free":true}\n')
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.write_bytes(refresh._receipt_bytes({**receipt, "unexpected": True}))
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.write_bytes(b"{" + b'"padding":"' + b"x" * refresh.MAX_RECEIPT_BYTES + b'"}')
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.unlink()
    target = config.receipt_root / "target.json"
    target.write_bytes(refresh._receipt_bytes(receipt))
    receipt_path.symlink_to(target)
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.unlink()
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    Path(config.registry_uri).write_text("changed\n")
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    Path(config.registry_uri).unlink()
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    non_published = {
        **receipt,
        "outcome": "failed",
        "operation_outcome": "failed",
        "reason": "provider_invalid",
        "operation_reason": "provider_invalid",
    }
    receipt_path.write_bytes(refresh._receipt_bytes(non_published))
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)


def test_current_receipt_validation_rejects_worker_registry_generation_mismatch(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    receipt_path, receipt = _write_current_published_receipt(config)
    shared = Path(config.registry_uri)
    worker = Path(config.worker_registry_uri)
    worker.write_bytes(shared.read_bytes())
    shared_preimage = capture_scheduler_provider_preimage(shared)
    worker_preimage = capture_scheduler_provider_preimage(worker)
    for provider in receipt["providers"]:
        if provider["name"] == "registry":
            provider["after_sha256"] = shared_preimage.sha256
        elif provider["name"] == "registry_worker_mirror":
            provider["after_sha256"] = worker_preimage.sha256
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    assert refresh.validate_current_receipt(config, receipt_path) == receipt

    worker.write_text("stale-worker-generation\n")
    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.validate_current_receipt(config, receipt_path)

    assert error_info.value.reason == "emergency_record_invalid"


def test_receipt_bounds_reject_long_strings_and_unknown_outcomes() -> None:
    receipt = refresh._receipt(
        run_id="run",
        started=refresh.datetime.now(refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="complete",
        providers=[],
    )
    refresh._validate_receipt(receipt)
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "outcome": "partial"})
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "phase": "x" * 513})
    boundary = {
        "items": ["package:" + "a" * 32],
        "total": refresh.MAX_ORPHANS,
        "discovered_total": refresh.MAX_ORPHANS,
        "attempted_total": refresh.MAX_ORPHANS,
        "created_total": refresh.MAX_ORPHANS,
        "truncated": True,
    }
    refresh._validate_receipt({**receipt, "orphans": boundary})
    with pytest.raises(ValueError):
        refresh._validate_receipt(
            {
                **receipt,
                "orphans": {
                    **boundary,
                    "total": refresh.MAX_ORPHANS + 1,
                    "discovered_total": refresh.MAX_ORPHANS + 1,
                    "attempted_total": refresh.MAX_ORPHANS + 1,
                    "created_total": refresh.MAX_ORPHANS + 1,
                },
            }
        )
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "residues": ["../outside"]})
    with pytest.raises(ValueError):
        refresh._receipt_bytes({"padding": "x" * refresh.MAX_RECEIPT_BYTES})
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "unexpected": True})
    with pytest.raises(ValueError):
        refresh._validate_receipt(
            {**receipt, "orphans": {**receipt["orphans"], "unexpected": True}}
        )


def test_receipt_schema_and_runtime_reject_same_expressible_negative_corpus() -> None:
    provider = {
        "name": "registry",
        "before_sha256": "a" * 64,
        "before_inode": 1,
        "before_schema_version": "v1",
        "before_generated_at": "2026-07-14T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }
    receipt = refresh._receipt(
        run_id="refresh_valid",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "registry_worker_mirror"},
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    refresh._validate_receipt(receipt)
    validator.validate(receipt)
    boundary = {
        **receipt,
        "orphans": {
            "items": [f"package:{index:032x}" for index in range(256)],
            "total": 4096,
            "discovered_total": 4096,
            "attempted_total": 4096,
            "created_total": 4096,
            "truncated": True,
        },
    }
    refresh._validate_receipt(boundary)
    validator.validate(boundary)
    overflow = {
        **boundary,
        "orphans": {
            **boundary["orphans"],
            "total": 4097,
            "discovered_total": 4097,
            "attempted_total": 4097,
            "created_total": 4097,
        },
    }
    with pytest.raises(ValueError):
        refresh._validate_receipt(overflow)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(overflow)
    invalid_receipts = [
        {**receipt, "run_id": "/"},
        {
            **receipt,
            "providers": [{**receipt["providers"][0], "after_sha256": "a"}, *receipt["providers"][1:]],
        },
        {**receipt, "providers": list(reversed(receipt["providers"]))},
    ]
    for invalid in invalid_receipts:
        with pytest.raises(ValueError):
            refresh._validate_receipt(invalid)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)


def test_receipt_latest_is_monotonic_and_history_keeps_both(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    newer = refresh._receipt(
        run_id="refresh_newer",
        started=refresh.datetime(2026, 7, 14, 7, tzinfo=refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="precommit",
        providers=[],
    )
    older = refresh._receipt(
        run_id="refresh_older",
        started=refresh.datetime(2026, 7, 14, 6, tzinfo=refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="precommit",
        providers=[],
    )

    refresh._publish_primary_receipt(root, newer)
    refresh._publish_primary_receipt(root, older)

    assert json.loads((root / "latest.json").read_text()) == newer
    assert {path.stem for path in (root / "history").iterdir()} == {"refresh_newer", "refresh_older"}


def test_concurrent_receipt_publishers_keep_exact_newest_32(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    receipts = [
        refresh._receipt(
            run_id=f"refresh_{index:02d}",
            started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=[],
        )
        for index in range(40)
    ]
    barrier = threading.Barrier(len(receipts))
    errors: list[BaseException] = []

    def publish(receipt: dict[str, object]) -> None:
        try:
            barrier.wait()
            refresh._publish_primary_receipt(root, receipt)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(receipt,)) for receipt in receipts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert json.loads((root / "latest.json").read_text())["run_id"] == "refresh_39"
    assert {path.stem for path in (root / "history").iterdir()} == {
        f"refresh_{index:02d}" for index in range(8, 40)
    }


def test_emergency_slot_handles_short_writes_and_validates_complete_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "emergency"
    root.mkdir(mode=0o700)
    receipt = refresh._receipt(
        run_id="refresh_short_writes",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=[],
    )
    slot = refresh._reserve_emergency_slot(root, str(receipt["run_id"]))
    real_write = os.write

    def short_write(fd: int, content: object) -> int:
        return real_write(fd, memoryview(content)[:1])

    monkeypatch.setattr(refresh.os, "write", short_write)
    refresh._finalize_emergency_slot(slot, receipt)

    recovered = json.loads((root / "refresh_short_writes.reserved.json").read_text())
    assert refresh._validate_receipt(recovered) == receipt


def test_full_refresh_short_writes_still_publish_complete_emergency_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    real_write = os.write

    def short_write(fd: int, content: object) -> int:
        return real_write(fd, memoryview(content)[:3])

    monkeypatch.setattr(refresh.os, "write", short_write)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published_receipt_failed"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert refresh._validate_receipt(json.loads(emergency[0].read_text()))["run_id"] == receipt["run_id"]


def test_full_refresh_zero_progress_emergency_write_is_uncertain_and_leak_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    captured: list[refresh.EmergencySlot] = []
    real_reserve = refresh._reserve_emergency_slot

    def capture_reserve(root: Path, run_id: str) -> refresh.EmergencySlot:
        slot = real_reserve(root, run_id)
        captured.append(slot)
        return slot

    monkeypatch.setattr(refresh, "_reserve_emergency_slot", capture_reserve)
    monkeypatch.setattr(refresh.os, "write", lambda fd, content: 0)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"
    assert list(config.emergency_root.iterdir()) == []
    assert captured
    for fd in (captured[0].file_fd, captured[0].parent_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure_target", ["file", "parent"])
def test_full_refresh_finalize_fsync_failure_is_uncertain_and_leak_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    captured: list[refresh.EmergencySlot] = []
    real_reserve = refresh._reserve_emergency_slot
    real_fsync = os.fsync

    def capture_reserve(root: Path, run_id: str) -> refresh.EmergencySlot:
        slot = real_reserve(root, run_id)
        captured.append(slot)
        return slot

    def fail_finalize_fsync(fd: int) -> None:
        if captured:
            target_fd = captured[0].file_fd if failure_target == "file" else captured[0].parent_fd
            if fd == target_fd:
                raise OSError(f"injected {failure_target} fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(refresh, "_reserve_emergency_slot", capture_reserve)
    monkeypatch.setattr(refresh.os, "fsync", fail_finalize_fsync)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"
    assert list(config.emergency_root.iterdir()) == []
    for fd in (captured[0].file_fd, captured[0].parent_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure_call", [1, 2])
def test_full_refresh_reserve_fsync_failure_cleans_workspace_slot_and_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    config = _config(tmp_path)
    opened: dict[str, int] = {}
    real_open_directory = refresh.open_directory_no_follow
    real_open = os.open
    real_fsync = os.fsync
    fsync_calls = 0

    def capture_parent(*args: object, **kwargs: object) -> int:
        fd = real_open_directory(*args, **kwargs)
        opened["parent"] = fd
        return fd

    def capture_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            opened["file"] = fd
        return fd

    def fail_reserve_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == failure_call:
            raise OSError("injected reserve fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(refresh, "open_directory_no_follow", capture_parent)
    monkeypatch.setattr(refresh.os, "open", capture_file)
    monkeypatch.setattr(refresh.os, "fsync", fail_reserve_fsync)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.reason == "primary_receipt_failed"
    assert list(config.emergency_root.iterdir()) == []
    assert list(config.workspace_root.iterdir()) == []
    assert set(opened) == {"parent", "file"}
    for fd in opened.values():
        with pytest.raises(OSError):
            os.fstat(fd)


def test_emergency_reservation_is_durable_before_first_provider_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    events: list[str] = []
    opened: dict[str, int] = {}
    real_open_directory = refresh.open_directory_no_follow
    real_open = os.open
    real_fsync = os.fsync
    registry_publisher = refresh.publish_all_basin_scheduler_registry

    def capture_parent(*args: object, **kwargs: object) -> int:
        fd = real_open_directory(*args, **kwargs)
        opened["parent"] = fd
        return fd

    def capture_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            opened["file"] = fd
        return fd

    def record_fsync(fd: int) -> None:
        if fd == opened.get("file"):
            events.append("reserve_file_fsync")
        elif fd == opened.get("parent"):
            events.append("reserve_parent_fsync")
        real_fsync(fd)

    def record_provider(**kwargs: object) -> dict[str, object]:
        events.append("provider_side_effect")
        return registry_publisher(**kwargs)

    monkeypatch.setattr(refresh, "open_directory_no_follow", capture_parent)
    monkeypatch.setattr(refresh.os, "open", capture_file)
    monkeypatch.setattr(refresh.os, "fsync", record_fsync)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", record_provider)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert events[:3] == ["reserve_file_fsync", "reserve_parent_fsync", "provider_side_effect"]


def test_workspace_bounds_reject_depth_entry_bytes_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "data").write_bytes(b"12345")
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_DEPTH", 1)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_DEPTH", 32)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_BYTES", 4)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_BYTES", 100)
    (root / "unsafe").symlink_to(nested)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)


def test_workspace_budget_rejects_oversized_copy_before_file_creation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "too-large.bin").write_bytes(b"12345")
    budget = refresh._WorkspaceBudget(root, max_bytes=4, max_entries=10, max_depth=10)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert not (root / "copy" / "too-large.bin").exists()


def test_workspace_budget_rejects_entry_before_creating_over_limit_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "second-entry.txt").write_text("content", encoding="utf-8")
    budget = refresh._WorkspaceBudget(root, max_bytes=100, max_entries=1, max_depth=10)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert (root / "copy").is_dir()
    assert not (root / "copy" / "second-entry.txt").exists()


def test_workspace_budget_rejects_depth_before_creating_over_limit_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    budget = refresh._WorkspaceBudget(root, max_bytes=100, max_entries=10, max_depth=1)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert (root / "copy").is_dir()
    assert not (root / "copy" / "nested").exists()


def test_workspace_budget_rejects_inventory_write_before_file_creation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    budget = refresh._WorkspaceBudget(root, max_bytes=1, max_entries=10, max_depth=10)
    inventory = root / "registry" / "basins-inventory.json"

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.write_json(inventory, {"models": []})

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert not inventory.exists()


def test_refresh_preflight_requires_private_euid_owned_lock_parent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.refresh_lock.parent.chmod(0o755)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_refresh_preflight_rejects_lock_parent_owned_by_another_euid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    owner = config.refresh_lock.parent.stat().st_uid
    monkeypatch.setattr(refresh.os, "geteuid", lambda: owner + 1)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_config_rejects_database_or_relative_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        "NHMS_BASINS_ROOT": str(tmp_path / "Basins"),
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "registry.json"),
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "objects/worker-registry.json"),
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": str(tmp_path / "readiness.json"),
        "NHMS_SCHEDULER_STATE_INDEX": str(tmp_path / "state.json"),
        "OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": str(tmp_path / "objects"),
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": str(tmp_path / "work"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": str(tmp_path / "emergency"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": str(tmp_path / "refresh"),
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        monkeypatch.setenv(selector, "redacted")
        with pytest.raises(refresh.RefreshError):
            refresh.RefreshConfig.from_env()
        monkeypatch.delenv(selector)
    monkeypatch.setenv("NHMS_BASINS_ROOT", "relative/Basins")
    with pytest.raises(refresh.RefreshError):
        refresh.RefreshConfig.from_env()


def test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.service").read_text()
    timer = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.timer").read_text()
    environment = (root / "infra/env/compute.scheduler-provider-refresh.env.example").read_text()
    wrapper = (root / "scripts/scheduler_file_provider_refresh_once.sh").read_text()
    installer = (root / "scripts/install_node22_scheduler_file_provider_refresh.sh").read_text()

    assert "ExecStart=/scratch/frd_muziyao/NWM/scripts/scheduler_file_provider_refresh_once.sh" in service
    assert "TimeoutStartSec=7200" in service
    assert "OnCalendar=*-*-* 02:15:00 UTC" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL" in service
    assert "PGPASSWORD" in service and "PGSSLROOTCERT" in service
    assert "Before=nhms-compute-scheduler.service" in service
    assert (
        "ExecCondition=/bin/sh -c '! /usr/bin/systemctl --user is-active --quiet "
        "nhms-compute-scheduler.service'" in service
    )
    assert "is-active --quiet nhms-compute-scheduler.service" in wrapper
    for selector in ("DATABASE_URL=", "PIPELINE_DATABASE_URL=", "PGHOST=", "PGPORT="):
        assert selector not in environment
    assert "stat -c '%a'" in wrapper
    assert "DATABASE_URL PIPELINE_DATABASE_URL PGAPPNAME" in wrapper
    assert "cmp -s" in installer
    assert "scheduler_unchanged" in installer
    assert "rollback_files" in installer and "restore_refresh_state" in installer
    assert "--validate-current-receipt" in installer
    assert "assert_refresh_service_inactive" in installer
    assert "stat -c '%a'" in installer
    assert installer.index("stat -c '%a'") < installer.index("stat -f '%Lp'")
    assert "stat -c '%a'" in wrapper
    assert wrapper.index("stat -c '%a'") < wrapper.index("stat -f '%Lp'")
    assert 'unit_state "$timer"' in installer and 'unit_state "$service"' in installer
    assert 'restore_unit_state "$timer"' in installer and 'restore_unit_state "$service"' in installer
    assert "enable --now \"$timer\"" in installer
    assert "enable --now nhms-compute-scheduler" not in installer
    assert "Persistent=false" in timer
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        assert selector in service
        assert selector in wrapper
        assert selector in installer


def test_env_file_loader_rejects_duplicate_keys_and_cli_rejects_conflicting_operations(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "refresh.env"
    env_file.write_text("OBJECT_STORE_ROOT=/private\nOBJECT_STORE_ROOT=/other\n")

    with pytest.raises(refresh.RefreshError, match="configuration_invalid"):
        refresh._apply_environment_file(env_file)
    with pytest.raises(SystemExit):
        refresh._build_parser().parse_args(
            [
                "--recover-emergency",
                str(tmp_path / "emergency.json"),
                "--validate-current-receipt",
                str(tmp_path / "latest.json"),
            ]
        )


def test_installer_enable_lifecycle_and_failure_restore_with_fake_systemctl(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    source_units = root / "infra/systemd"
    units = repo / "infra/systemd"
    env_dir = repo / "infra/env"
    scripts_dir = repo / "scripts"
    units.mkdir(parents=True)
    env_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    shutil.copy2(root / "scripts/scheduler_file_provider_refresh.py", scripts_dir)
    # The #1080 cutover-declaration schema is loaded at module import; the
    # installer runs the script from repo/scripts/ so schemas/ must resolve
    # relative to the copied tree too.
    (repo / "schemas").mkdir()
    shutil.copy2(
        root / "schemas/scheduler_registry_package_cutover.schema.json",
        repo / "schemas",
    )
    for name in (
        "nhms-scheduler-file-provider-refresh.service",
        "nhms-scheduler-file-provider-refresh.timer",
    ):
        shutil.copy2(source_units / name, units / name)
    basins = tmp_path / "Basins"
    private_objects = tmp_path / "private-objects"
    shared_providers = tmp_path / "shared-providers"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, private_objects, shared_providers, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (tmp_path / "private", work, receipts, emergency):
        path.chmod(0o700)
    provider_paths = {
        "registry": shared_providers / "scheduler/registry/manifest-last.json",
        "registry_worker_mirror": private_objects / "scheduler/registry/manifest-last.json",
        "readiness": shared_providers / "scheduler/canonical-readiness/index-last.json",
        "state": shared_providers / "scheduler/state-index/index-last.json",
    }
    providers = []
    for name, path in provider_paths.items():
        path.parent.mkdir(parents=True)
        path.write_text("registry\n" if name == "registry_worker_mirror" else name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = receipts / "latest.json"
    receipt.write_bytes(
        refresh._receipt_bytes(
            refresh._receipt(
                run_id="refresh_installer",
                started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
                outcome="published",
                reason="success",
                phase="complete",
                providers=providers,
                registry_classification=_classification_stub(),
            )
        )
    )
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text(
        "\n".join(
            (
                f"NHMS_BASINS_ROOT={basins}",
                f"OBJECT_STORE_ROOT={private_objects}",
                f"NHMS_SCHEDULER_PROVIDER_STORE_ROOT={shared_providers}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"NHMS_SCHEDULER_REGISTRY_MANIFEST={provider_paths['registry']}",
                f"NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST={provider_paths['registry_worker_mirror']}",
                f"NHMS_SCHEDULER_CANONICAL_READINESS_INDEX={provider_paths['readiness']}",
                f"NHMS_SCHEDULER_STATE_INDEX={provider_paths['state']}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT={work}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT={receipts}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT={emergency}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK={tmp_path / 'private/refresh'}",
            )
        )
        + "\n"
    )
    env_file.chmod(0o600)
    fake_state = tmp_path / "systemctl-state.json"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "path = os.environ['FAKE_SYSTEMCTL_STATE']\n"
        "try:\n"
        "    state = json.load(open(path, encoding='utf-8'))\n"
        "except FileNotFoundError:\n"
        "    state = {}\n"
        "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
        "command = args[0]\n"
        "unit = args[-1] if len(args) > 1 else ''\n"
        "with open(os.environ['FAKE_SYSTEMCTL_TRACE'], 'a', encoding='utf-8') as trace:\n"
        "    trace.write(' '.join(args) + '\\n')\n"
        "current = state.setdefault(unit, {'enabled':'disabled','active':'inactive'})\n"
        "if command == 'is-enabled': print(current['enabled'])\n"
        "elif command == 'is-active': print(current['active'])\n"
        "elif command == 'enable':\n"
        "    current['enabled'] = 'enabled'\n"
        "    if '--now' in args: current['active'] = 'active'\n"
        "elif command == 'disable':\n"
        "    current['enabled'] = 'disabled'\n"
        "    if '--now' in args: current['active'] = 'inactive'\n"
        "elif command == 'start': current['active'] = 'active'\n"
        "elif command == 'stop': current['active'] = 'inactive'\n"
        "elif command != 'daemon-reload': raise SystemExit(2)\n"
        "with open(path, 'w', encoding='utf-8') as handle: json.dump(state, handle)\n"
        "if os.environ.get('FAKE_FAIL_AFTER') == command + ':' + unit: raise SystemExit(9)\n"
    )
    fake_systemctl.chmod(0o755)
    fake_trace = tmp_path / "systemctl-trace.log"
    unit_dir = tmp_path / "units"
    state_root = tmp_path / "install-state"
    environment = {
        **os.environ,
        "FAKE_SYSTEMCTL_STATE": str(fake_state),
        "FAKE_SYSTEMCTL_TRACE": str(fake_trace),
        "NHMS_SCHEDULER_REFRESH_REPO": str(repo),
        "NHMS_SCHEDULER_REFRESH_UNIT_DIR": str(unit_dir),
        "NHMS_SCHEDULER_REFRESH_INSTALL_STATE_ROOT": str(state_root),
        "NHMS_SCHEDULER_REFRESH_SYSTEMCTL": str(fake_systemctl),
        "NHMS_SCHEDULER_REFRESH_PYTHON": sys.executable,
        "NHMS_SCHEDULER_REFRESH_RECEIPT": str(receipt),
        "PYTHONPATH": str(root),
    }
    installer = root / "scripts/install_node22_scheduler_file_provider_refresh.sh"

    subprocess.run([str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    enabled = subprocess.run(
        [str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )

    assert json.loads(enabled.stdout)["status"] == "enabled_active"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    assert state["nhms-scheduler-file-provider-refresh.service"]["active"] == "inactive"

    invalid_receipt = receipt.read_bytes()
    receipt.write_text('{"outcome":"published","database_free":true}\n')
    failed = subprocess.run(
        [str(installer), "--enable"], env=environment, check=False, capture_output=True, text=True
    )
    assert failed.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    receipt.write_bytes(invalid_receipt)

    repeated = subprocess.run(
        [str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(repeated.stdout)["status"] == "enabled_active"

    rolled_back = subprocess.run(
        [str(installer), "--rollback"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(rolled_back.stdout)["status"] == "rolled_back"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.service"] == {"enabled": "disabled", "active": "inactive"}

    subprocess.run([str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    fail_environment = {
        **environment,
        "FAKE_FAIL_AFTER": "enable:nhms-scheduler-file-provider-refresh.timer",
    }
    failed_after_enable = subprocess.run(
        [str(installer), "--enable"], env=fail_environment, check=False, capture_output=True, text=True
    )
    assert failed_after_enable.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}

    for transitional in ("activating", "deactivating", "reloading", "active"):
        state["nhms-scheduler-file-provider-refresh.service"] = {
            "enabled": "disabled",
            "active": transitional,
        }
        fake_state.write_text(json.dumps(state))
        fake_trace.write_text("")
        refused = subprocess.run(
            [str(installer), "--install"], env=environment, check=False, capture_output=True, text=True
        )
        assert refused.returncode != 0
        assert all(
            not line.startswith(("enable ", "disable ", "start ", "stop ", "daemon-reload"))
            for line in fake_trace.read_text().splitlines()
        )


def _write_wrapper_execution_fixture(tmp_path: Path, *, include_forbidden: bool = False) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    env_dir = repo / "infra/env"
    interpreter = repo / ".venv/bin/python"
    env_dir.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    configured = {
        "NHMS_BASINS_ROOT": "/trusted/Basins",
        "OBJECT_STORE_ROOT": "/trusted/object-store",
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": "/trusted/provider-store",
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": "/trusted/object-store/scheduler/registry/manifest-last.json",
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST": "/trusted/object-store/scheduler/registry/worker-manifest-last.json",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": "/trusted/object-store/scheduler/readiness/index-last.json",
        "NHMS_SCHEDULER_STATE_INDEX": "/trusted/object-store/scheduler/state/index-last.json",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": "/private/work",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": "/private/receipts",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": "/private/emergency",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": "/private/refresh",
    }
    lines = [f"{key}={value}" for key, value in configured.items()]
    if include_forbidden:
        lines.append("DATABASE_URL=must-not-load")
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    marker = tmp_path / "interpreter-ran"
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {marker}\n"
        "printf 'BASINS=%s\\n' \"$NHMS_BASINS_ROOT\"\n"
        "printf 'OBJECTS=%s\\n' \"$OBJECT_STORE_ROOT\"\n"
        "printf 'PREFIX=%s\\n' \"$OBJECT_STORE_PREFIX\"\n"
        "printf 'REGISTRY=%s\\n' \"$NHMS_SCHEDULER_REGISTRY_MANIFEST\"\n"
        "printf 'WORKER_REGISTRY=%s\\n' \"$NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST\"\n"
        "printf 'READINESS=%s\\n' \"$NHMS_SCHEDULER_CANONICAL_READINESS_INDEX\"\n"
        "printf 'STATE=%s\\n' \"$NHMS_SCHEDULER_STATE_INDEX\"\n"
        "printf 'WORK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT\"\n"
        "printf 'RECEIPTS=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT\"\n"
        "printf 'EMERGENCY=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT\"\n"
        "printf 'LOCK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK\"\n"
        "printf 'DATABASE_URL=%s\\n' \"${DATABASE_URL-<unset>}\"\n"
        "printf 'PGHOST=%s\\n' \"${PGHOST-<unset>}\"\n"
        "printf 'PWD=%s\\n' \"$PWD\"\n"
        "printf 'ARGS=%s\\n' \"$*\"\n"
    )
    interpreter.chmod(0o755)
    wrapper = tmp_path / "refresh-wrapper.sh"
    wrapper.write_text(
        (root / "scripts/scheduler_file_provider_refresh_once.sh")
        .read_text()
        .replace("repo=/scratch/frd_muziyao/NWM", f"repo={repo}")
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def test_wrapper_clean_environment_loads_fixed_config_and_strips_inherited_db_selectors(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "DATABASE_URL": "inherited-secret",
            "PGHOST": "inherited-host",
        },
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "BASINS=/trusted/Basins" in result.stdout
    assert "OBJECTS=/trusted/object-store" in result.stdout
    assert "PREFIX=s3://nhms" in result.stdout
    assert "REGISTRY=/trusted/object-store/scheduler/registry/manifest-last.json" in result.stdout
    assert (
        "WORKER_REGISTRY=/trusted/object-store/scheduler/registry/worker-manifest-last.json"
        in result.stdout
    )
    assert "READINESS=/trusted/object-store/scheduler/readiness/index-last.json" in result.stdout
    assert "STATE=/trusted/object-store/scheduler/state/index-last.json" in result.stdout
    assert "WORK=/private/work" in result.stdout
    assert "RECEIPTS=/private/receipts" in result.stdout
    assert "EMERGENCY=/private/emergency" in result.stdout
    assert "LOCK=/private/refresh" in result.stdout
    assert "DATABASE_URL=<unset>" in result.stdout
    assert "PGHOST=<unset>" in result.stdout
    assert f"PWD={tmp_path / 'repo'}" in result.stdout
    assert result.stdout.rstrip().endswith("-m scripts.scheduler_file_provider_refresh --dry-run")


def test_wrapper_rejects_forbidden_selector_in_mode_0600_env_before_exec(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path, include_forbidden=True)

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert not marker.exists()


# ---------------------------------------------------------------------------
# #1080 Registry Cutover Gate — direct classification and refusal coverage.
# ---------------------------------------------------------------------------


def _registry_row(
    model_id: str,
    package_checksum: str,
    *,
    basin_id: str | None = None,
    basin_version_id: str = "v1",
    river_network_version_id: str = "r1",
    shud_code_version: str = "basins-shud",
    segment_count: int = 100,
    output_segment_count: int = 50,
    lifecycle_state: str = "active",
    source_inventory_checksum: str | None = None,
    resource_profile_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Shape-realistic registry row covering every identity field the classifier checks.

    Defaults deliberately mirror what
    ``scripts.publish_scheduler_file_registry.scheduler_registry_row_from_sources``
    emits (see #1080 finding C-C1) so tests express drift on any of the
    documented identity fields without having to reconstruct the whole row.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/v1/manifest.json",
        "package_checksum": package_checksum,
        "model_package_uri": f"s3://nhms/models/{model_id}/v1/package.tgz",
        "source_inventory_checksum": source_inventory_checksum
        or f"sha256:{'0' * 62}{ord(model_id[-1]) & 0xFF:02x}",
    }
    if resource_profile_extra:
        profile.update(resource_profile_extra)
    return {
        "model_id": model_id,
        "basin_id": basin_id or f"basin-{model_id}",
        "basin_version_id": basin_version_id,
        "river_network_version_id": river_network_version_id,
        "shud_code_version": shud_code_version,
        "segment_count": segment_count,
        "output_segment_count": output_segment_count,
        "lifecycle_state": lifecycle_state,
        "model_package_uri": f"s3://nhms/models/{model_id}/v1/package.tgz",
        "manifest_uri": f"s3://nhms/models/{model_id}/v1/manifest.json",
        "package_checksum": package_checksum,
        "resource_profile": profile,
    }


def _assert_classification_reconciles(
    classification: dict[str, object],
    *,
    previous_count: int | None,
    prospective_count: int | None,
) -> None:
    """Assert every reconciliation formula from spec.md:397-403 (T3 / C-E4).

    * ``unchanged + package_changed + removed == previous_count`` when a
      previous canonical registry existed;
    * ``added + unchanged + package_changed == prospective_count`` unless
      the caller passes ``prospective_count=None`` (dry_run id-only mode);
    * ``declared_cutovers`` model_ids ⊆ ``package_changed`` model_ids;
    * ``refused`` ⊇ ``removed`` ∪ (``package_changed`` \\ ``declared_cutovers``).
    """
    def total(group: str) -> int:
        return int(classification[group]["total"])  # type: ignore[index]

    def ids(group: str) -> set[str]:
        return {
            str(item["model_id"])  # type: ignore[index]
            for item in classification[group]["items"]  # type: ignore[index]
            if isinstance(item, dict)
        }

    added = total("added")
    unchanged = total("unchanged")
    removed = total("removed")
    package_changed = total("package_changed")
    refused = total("refused")
    declared = total("declared_cutovers")

    if previous_count is not None:
        assert unchanged + package_changed + removed == previous_count, (
            f"previous reconciliation broken: unchanged={unchanged} "
            f"package_changed={package_changed} removed={removed} "
            f"previous_count={previous_count}"
        )
    if prospective_count is not None:
        assert added + unchanged + package_changed == prospective_count, (
            f"prospective reconciliation broken: added={added} "
            f"unchanged={unchanged} package_changed={package_changed} "
            f"prospective_count={prospective_count}"
        )
    assert ids("declared_cutovers") <= ids("package_changed")
    # refused >= removed + (package_changed - declared_cutovers)
    assert refused >= removed + max(package_changed - declared, 0)


def _valid_previous_manifest(models: list[dict[str, object]]) -> bytes:
    """Shape-valid canonical manifest bytes for gate tests."""
    return json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "models": models,
            "checksum": f"sha256:{'0' * 64}",
        },
        sort_keys=True,
    ).encode() + b"\n"


def _write_previous_canonical(config: refresh.RefreshConfig, models: list[dict[str, object]]) -> Path:
    path = Path(config.registry_uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_valid_previous_manifest(models))
    return path


def _write_declaration(
    tmp_path: Path,
    *,
    generation: str,
    entries: list[dict[str, object]],
    generated_at: str = "2026-07-14T12:00:00Z",
) -> Path:
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": generated_at,
                "generation": generation,
                "entries": entries,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return declaration


def _run_gate(
    tmp_path: Path,
    config: refresh.RefreshConfig,
    *,
    prospective_models: list[dict[str, object]],
    previous_models: list[dict[str, object]] | None = None,
    declaration_path: Path | None = None,
    dry_run: bool = False,
    generated_at: refresh.datetime | None = None,
    now: refresh.datetime | None = None,
) -> tuple[list[dict[str, object]], Exception | None]:
    workspace = tmp_path / "gate-workspace"
    workspace.mkdir(exist_ok=True)
    generated_at = generated_at or refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    now = now or generated_at
    registry_path = Path(config.registry_uri)
    if previous_models is not None:
        # Only overwrite if the caller has NOT already staged a specific
        # bytes snapshot on disk.  Rewriting bumps mtime which round-2
        # tests (T8 / C-E9) need to survive across the gate call.
        if not registry_path.exists():
            _write_previous_canonical(config, previous_models)
        previous_bytes = registry_path.read_bytes()
        previous_sha = refresh.hashlib.sha256(previous_bytes).hexdigest()
    else:
        previous_bytes = None
        previous_sha = None
    captured: list[dict[str, object]] = []

    def sink(payload: dict[str, object]) -> None:
        captured.append(payload)

    caught: Exception | None = None
    try:
        refresh._registry_precommit_gate(
            workspace,
            [],
            prospective_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=previous_sha,
            prospective_generated_at=generated_at,
            cutover_declaration_env=str(declaration_path) if declaration_path else None,
            dry_run=dry_run,
            classification_sink=sink,
            now=now,
        )
    except Exception as error:  # noqa: BLE001 -- intentional
        caught = error
    return captured, caught


def test_cutover_gate_admits_prospective_superset_with_byte_identical_existing_rows(
    tmp_path: Path,
) -> None:
    """(a) 13 previous + 6 new -> 6 added + 13 unchanged, refresh proceeds."""
    config = _config(tmp_path)
    previous = [
        _registry_row(f"basin-1{index:02d}", "a" * 64)
        for index in range(1, 14)
    ]
    prospective = previous + [
        _registry_row(f"basin-2{index:02d}", "b" * 64)
        for index in range(1, 7)
    ]

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
    )

    assert error is None
    payload = captured[0]
    assert payload["added"]["total"] == 6
    assert payload["unchanged"]["total"] == 13
    assert payload["removed"]["total"] == 0
    assert payload["package_changed"]["total"] == 0
    assert payload["refused"]["total"] == 0
    assert payload["declared_cutovers"]["total"] == 0
    assert payload["previous_registry_sha256"] is not None
    assert payload["new_registry_sha256"] is not None


def test_cutover_gate_refuses_undeclared_package_checksum_drift(tmp_path: Path) -> None:
    """(b) 1 existing package_changed without declaration -> refusal + previous intact.

    Also asserts (T8 / C-E9) that inode and mtime of the previous canonical
    file survive the refusal, not just the byte content.
    """
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),  # checksum drift, undeclared
        _registry_row("basin-102", "b" * 64),
    ]
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    assert error.details["provider_phase"] == "precommit"
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["refused"]["items"][0]["reason"] == "registry_cutover_undeclared"
    assert payload["refused"]["items"][0]["model_id"] == "basin-101"
    assert payload["refused"]["items"][0]["old_checksum"] == "a" * 64
    assert payload["refused"]["items"][0]["new_checksum"] == "c" * 64
    # Previous canonical bytes, inode, and mtime untouched.
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    _assert_classification_reconciles(
        payload, previous_count=2, prospective_count=2
    )


def test_cutover_gate_admits_valid_declaration_for_specific_checksum_transition(
    tmp_path: Path,
) -> None:
    """(c) Valid declaration accepts the same transition."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    generation = refresh._prospective_registry_generation(
        prospective, generated_at=generated_at
    )
    declaration = _write_declaration(
        tmp_path,
        generation=generation,
        entries=[
            {
                "model_id": "basin-101",
                "old_checksum": "a" * 64,
                "new_checksum": "c" * 64,
                "effective_cycle_utc": "2026-07-15T00:00:00Z",
                "transition_mode": "replace",
            }
        ],
    )

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        declaration_path=declaration,
        generated_at=generated_at,
        now=generated_at,
    )

    assert error is None, error
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["declared_cutovers"]["total"] == 1
    assert payload["declared_cutovers"]["items"][0]["model_id"] == "basin-101"
    assert payload["declared_cutovers"]["items"][0]["transition_mode"] == "replace"
    assert payload["refused"]["total"] == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "schema_invalid",
        "wrong_generation",
        "wrong_old_checksum",
        "wrong_new_checksum",
        "non_cycle_aligned",
        "out_of_window_past",
        "out_of_window_future",
        "duplicate_model_id",
        "unknown_model_id",
        "symlinked_declaration",
        "over_size_declaration",
    ],
)
def test_cutover_gate_rejects_invalid_declaration_modes(
    tmp_path: Path, corruption: str
) -> None:
    """(d) Every invalid declaration mode fails closed."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    prospective = [
        _registry_row("basin-101", "c" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    generation = refresh._prospective_registry_generation(
        prospective, generated_at=generated_at
    )
    valid_entry = {
        "model_id": "basin-101",
        "old_checksum": "a" * 64,
        "new_checksum": "c" * 64,
        "effective_cycle_utc": "2026-07-15T00:00:00Z",
        "transition_mode": "replace",
    }
    declaration_path: Path
    if corruption == "schema_invalid":
        # Missing the schema_version key entirely.
        declaration_path = tmp_path / "declaration.json"
        declaration_path.write_text('{"entries": []}\n')
    elif corruption == "symlinked_declaration":
        target = tmp_path / "real-declaration.json"
        target.write_text(
            json.dumps(
                {
                    "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                    "generated_at": "2026-07-14T00:00:00Z",
                    "generation": generation,
                    "entries": [valid_entry],
                },
                sort_keys=True,
            )
        )
        declaration_path = tmp_path / "declaration.json"
        declaration_path.symlink_to(target)
    elif corruption == "over_size_declaration":
        declaration_path = tmp_path / "declaration.json"
        oversized = {
            "schema_version": "nhms.scheduler.registry_package_cutover.v1",
            "generated_at": "2026-07-14T00:00:00Z",
            "generation": generation,
            "entries": [valid_entry],
        }
        # Inflate the payload well past MAX_CUTOVER_DECLARATION_BYTES via
        # padding-embedded entries that still schema-validate.  The bounded
        # no-follow read rejects the file before parsing.
        entries = []
        for index in range(300):  # >256-cap forces schema-invalidity too
            entries.append(
                {
                    "model_id": f"basin-{index:03d}",
                    "old_checksum": "a" * 64,
                    "new_checksum": "c" * 64,
                    "effective_cycle_utc": "2026-07-15T00:00:00Z",
                    "transition_mode": "replace",
                }
            )
        oversized["entries"] = entries
        raw = (json.dumps(oversized, sort_keys=True) + "\n").encode()
        # Pad to actually exceed the byte cap.
        raw = raw + b" " * (refresh.MAX_CUTOVER_DECLARATION_BYTES + 1024)
        declaration_path.write_bytes(raw)
    else:
        entry = dict(valid_entry)
        gen_for_file = generation
        if corruption == "wrong_generation":
            gen_for_file = "manifest-99999999-deadbeefcafe"
        elif corruption == "wrong_old_checksum":
            entry["old_checksum"] = "9" * 64
        elif corruption == "wrong_new_checksum":
            entry["new_checksum"] = "9" * 64
        elif corruption == "non_cycle_aligned":
            entry["effective_cycle_utc"] = "2026-07-15T06:00:00Z"
        elif corruption == "out_of_window_past":
            entry["effective_cycle_utc"] = "2026-07-10T00:00:00Z"
        elif corruption == "out_of_window_future":
            entry["effective_cycle_utc"] = "2027-01-01T00:00:00Z"
        elif corruption == "duplicate_model_id":
            declaration_path = _write_declaration(
                tmp_path,
                generation=gen_for_file,
                entries=[valid_entry, dict(valid_entry)],
            )
        elif corruption == "unknown_model_id":
            entry["model_id"] = "basin-does-not-exist"
        if corruption not in {"duplicate_model_id"}:
            declaration_path = _write_declaration(
                tmp_path, generation=gen_for_file, entries=[entry]
            )

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        declaration_path=declaration_path,
        generated_at=generated_at,
        now=generated_at,
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_declaration_invalid"
    assert error.details["provider_phase"] == "precommit"
    # Previous canonical bytes, inode, and mtime untouched (T8 / C-E9).
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )


def test_cutover_gate_refuses_removed_previously_canonical_model(tmp_path: Path) -> None:
    """(e) Previously canonical model removed -> refusal, previous intact.

    Explicitly asserts (T7 / C-E8) that the removed row's refused entry
    carries the previous checksum in ``old_checksum`` and ``null`` in
    ``new_checksum``, and asserts (T8 / C-E9) that inode+mtime survive.
    """
    config = _config(tmp_path)
    previous = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    prospective = [_registry_row("basin-101", "a" * 64)]  # basin-102 dropped
    registry_path = _write_previous_canonical(config, previous)
    before = registry_path.read_bytes()
    before_stat = registry_path.stat()

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_removal_refused"
    payload = captured[0]
    assert payload["removed"]["items"] == ["basin-102"]
    refused_removed = next(
        item
        for item in payload["refused"]["items"]
        if item["reason"] == "registry_cutover_removal_refused"
    )
    assert refused_removed["model_id"] == "basin-102"
    # Symmetric to the drift branch: previous checksum is populated, no new
    # checksum exists on the prospective side (T7 / C-E8).
    assert refused_removed["old_checksum"] == "b" * 64
    assert refused_removed["new_checksum"] is None
    # bytes / inode / mtime survive (T8 / C-E9).
    assert registry_path.read_bytes() == before
    after_stat = registry_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    _assert_classification_reconciles(
        payload, previous_count=2, prospective_count=1
    )


def test_cutover_gate_missing_previous_canonical_is_first_publication(tmp_path: Path) -> None:
    """(f) Missing previous canonical registry -> every row is `added`."""
    config = _config(tmp_path)
    prospective = [
        _registry_row("basin-a", "a" * 64),
        _registry_row("basin-b", "b" * 64),
    ]
    # Do not write any previous manifest.
    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=None
    )

    assert error is None
    payload = captured[0]
    assert payload["previous_registry_sha256"] is None
    assert payload["added"]["total"] == 2
    assert set(payload["added"]["items"]) == {"basin-a", "basin-b"}
    assert payload["unchanged"]["total"] == 0
    assert payload["removed"]["total"] == 0


def test_cutover_classification_bounded_evidence_truncates_over_cap(tmp_path: Path) -> None:
    """Classification arrays cap at 256 with total + truncated fields."""
    config = _config(tmp_path)
    # 300 previous canonical models, all removed in the prospective set.
    previous = [_registry_row(f"basin-{index:03d}", "a" * 64) for index in range(300)]
    prospective: list[dict[str, object]] = []
    _write_previous_canonical(config, previous)

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    # Empty prospective is technically valid classification (all removed).
    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_removal_refused"
    payload = captured[0]
    assert payload["removed"]["total"] == 300
    assert payload["removed"]["truncated"] is True
    assert len(payload["removed"]["items"]) == refresh.MAX_COLLECTION_ITEMS
    assert payload["refused"]["total"] == 300
    assert payload["refused"]["truncated"] is True
    assert len(payload["refused"]["items"]) == refresh.MAX_COLLECTION_ITEMS


def test_cutover_gate_dry_run_reports_id_only_classification_without_refusal(
    tmp_path: Path,
) -> None:
    """dry_run: id-only classification, no refusal even if drift-shaped."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64)]
    # In dry-run mode the prospective rows carry only id/basin_id.
    prospective = [
        {"model_id": "basin-101", "basin_id": "basin-basin-101"},
        {"model_id": "basin-201", "basin_id": "basin-201"},
    ]

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=prospective,
        previous_models=previous,
        dry_run=True,
    )

    assert error is None
    payload = captured[0]
    assert payload["added"]["items"] == ["basin-201"]
    assert payload["unchanged"]["items"] == ["basin-101"]
    assert payload["package_changed"]["total"] == 0
    assert payload["new_registry_sha256"] is None  # dry_run does not publish


def test_refresh_receipt_binds_registry_classification_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful published receipt carries a `registry_classification` payload."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    # Seed one previous canonical model so classification has real content.
    previous_models = [_registry_row("model-1", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline(monkeypatch)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    classification = receipt["registry_classification"]
    assert classification["previous_registry_sha256"] is not None
    # Stubbed pipeline emits 13 minimal id-only rows.
    assert classification["added"]["total"] + classification["unchanged"]["total"] == 13


def test_refresh_receipt_binds_registry_classification_on_undeclared_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused refresh emits registry_classification pinpointing the drift."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("model-a", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline(monkeypatch)

    def publish_registry_with_drift(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [_registry_row("model-a", "c" * 64)],
        )
        return {"selected_model_count": 1, "registry": None, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry_with_drift)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    classification = receipt["registry_classification"]
    assert classification["refused"]["total"] == 1
    assert classification["refused"]["items"][0]["reason"] == "registry_cutover_undeclared"
    # Previous canonical registry SHA is captured on the receipt.
    assert classification["previous_registry_sha256"] is not None
    _assert_classification_reconciles(
        classification, previous_count=1, prospective_count=1
    )


def test_prospective_registry_generation_is_deterministic(
    tmp_path: Path,
) -> None:
    """Same models produce the same generation string; wall clock is excluded.

    Operators observe this value on a refused refresh receipt and file the
    matching cutover declaration; determinism (across wall-clock intervals,
    finding C-B1) is the operational contract that makes the refuse ->
    declare -> retry loop convergent.
    """
    models = [_registry_row("basin-101", "a" * 64), _registry_row("basin-102", "b" * 64)]
    generated_at = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    gen1 = refresh._prospective_registry_generation(models, generated_at=generated_at)
    gen2 = refresh._prospective_registry_generation(
        list(models), generated_at=generated_at
    )
    assert gen1 == gen2
    assert gen1.startswith("manifest-")
    # Generation is a pure content hash: shape is manifest-<12 hex chars>.
    _, _, digest = gen1.partition("-")
    assert len(digest) == 12
    assert all(character in "0123456789abcdef" for character in digest)
    # A different model set produces a different generation.
    perturbed = models + [_registry_row("basin-103", "c" * 64)]
    gen3 = refresh._prospective_registry_generation(perturbed, generated_at=generated_at)
    assert gen3 != gen1
    # Wall-clock changes MUST NOT change the generation for the same models
    # (T10, finding C-B1).  A generation that varies with wall clock breaks
    # the refuse-declaration-retry operator loop.
    later = refresh.datetime(2026, 7, 15, 9, 42, 17, 500000, tzinfo=refresh.UTC)
    much_later = refresh.datetime(2026, 7, 16, 3, 0, tzinfo=refresh.UTC)
    gen_later = refresh._prospective_registry_generation(models, generated_at=later)
    gen_much_later = refresh._prospective_registry_generation(
        models, generated_at=much_later
    )
    assert gen1 == gen_later == gen_much_later


# ---------------------------------------------------------------------------
# Round-2 fix-pass tests (T1-T13; see #1080 review round-1-verdicts-summary.md)
# ---------------------------------------------------------------------------


def _write_previous_manifest_with_generated_at(
    config: refresh.RefreshConfig,
    models: list[dict[str, object]],
    generated_at: str,
) -> Path:
    path = Path(config.registry_uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": generated_at,
                "models": models,
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    return path


def _stub_provider_pipeline_with_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prospective_models: list[dict[str, object]],
) -> None:
    """Version of `_stub_provider_pipeline` that emits caller-provided rows.

    The default `_stub_provider_pipeline` bakes a 13-row prospective set; the
    round-2 tests need to drive the runner with fully-shaped registry rows so
    published receipts carry real classification content.
    """
    preimage = ProviderPreimage(exists=False)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", lambda *args, **kwargs: preimage)
    monkeypatch.setattr(
        refresh,
        "_read_provider_header",
        lambda *args, **kwargs: {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-01T00:00:00Z",
            "checksum": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"entry": "valid"}], {"status": "ready", "entry_count": 26}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})

    class _Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", _Repository)

    committed_sha_holder: dict[str, str] = {}

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](workspace, [], prospective_models)
        # Simulate a canonical commit by writing the manifest bytes so
        # downstream provider evidence has a real SHA to bind to.
        if not kwargs.get("dry_run"):
            registry_uri = Path(str(kwargs["registry_manifest"]))
            registry_uri.parent.mkdir(parents=True, exist_ok=True)
            content, sha = refresh._prospective_registry_content(
                prospective_models,
                generated_at=kwargs.get("registry_generated_at")
                or refresh.datetime.now(refresh.UTC),
            )
            registry_uri.write_bytes(content)
            committed_sha_holder["sha"] = sha
            observer = kwargs.get("registry_commit_observer")
            if observer is not None:
                observer(
                    ProviderPreimage(
                        exists=True,
                        sha256=sha,
                        inode=registry_uri.stat().st_ino,
                        size=len(content),
                    )
                )
            return {
                "selected_model_count": len(prospective_models),
                "registry": {
                    "checksum": f"sha256:{sha}",
                    "model_count": len(prospective_models),
                    "content_sha256": sha,
                    "entry_count": len(prospective_models),
                },
                "packages": [],
            }
        return {
            "selected_model_count": len(prospective_models),
            "registry": None,
            "packages": [],
        }

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "e" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        refresh,
        "publish_state_snapshot_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "f" * 64, "entry_count": 1},
    )


def test_full_runner_published_receipt_binds_new_registry_sha_and_reconciles_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 / C-E2: full ``dry_run=False`` end-to-end publish carries a
    reconciled classification and ``new_registry_sha256`` equals the
    registry provider's ``after_sha256``."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [
        _registry_row("basin-101", "a" * 64),
        _registry_row("basin-102", "b" * 64),
    ]
    _write_previous_canonical(config, previous_models)
    # Prospective: keep both existing rows byte-identical + add one new row.
    prospective_models = previous_models + [_registry_row("basin-201", "c" * 64)]
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    classification = receipt["registry_classification"]
    registry_provider = next(
        provider for provider in receipt["providers"] if provider["name"] == "registry"
    )
    assert classification["new_registry_sha256"] == registry_provider["after_sha256"]
    _assert_classification_reconciles(
        classification, previous_count=2, prospective_count=3
    )
    assert classification["added"]["total"] == 1
    assert classification["unchanged"]["total"] == 2
    assert classification["package_changed"]["total"] == 0
    assert classification["refused"]["total"] == 0


def test_full_runner_published_receipt_admits_valid_cutover_and_still_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 / C-E3: 13 previous + 19 prospective where one existing model
    changes checksum with a valid declaration.  End-to-end runner path must
    reach ``outcome="published"`` with 6 added / 12 unchanged / 1
    package_changed / 1 declared_cutovers / 0 refused, and the strict
    receipt validator must accept the payload (round-1 finding C-A1)."""
    previous_models = [_registry_row(f"basin-1{i:02d}", "a" * 64) for i in range(1, 14)]
    # Prospective: 12 existing byte-identical + 1 existing with checksum
    # drift (basin-101 changes checksum) + 6 new basins.
    prospective_models = [
        _registry_row("basin-101", "c" * 64),  # package_changed via declaration
    ] + [
        _registry_row(f"basin-1{i:02d}", "a" * 64) for i in range(2, 14)
    ] + [
        _registry_row(f"basin-2{i:02d}", "b" * 64) for i in range(1, 7)
    ]
    # File the matching cutover declaration.
    generation = refresh._prospective_registry_generation(
        prospective_models, generated_at=refresh.datetime.now(refresh.UTC)
    )
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-07-14T00:00:00Z",
                "generation": generation,
                "entries": [
                    {
                        "model_id": "basin-101",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "effective_cycle_utc": "2026-07-15T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    declaration.chmod(0o600)
    monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))
    # Freeze wall clock via a subclass so refresh.datetime.now(UTC) returns
    # a fixed value (datetime.datetime is immutable and cannot have its
    # classmethod replaced directly — set the module-level attribute
    # instead).
    class _StubDateTime(refresh.datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return refresh.datetime(2026, 7, 14, 18, tzinfo=refresh.UTC)

    monkeypatch.setattr(refresh, "datetime", _StubDateTime)

    config = _config(tmp_path)
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    classification = receipt["registry_classification"]
    assert classification["added"]["total"] == 6
    assert classification["unchanged"]["total"] == 12
    assert classification["package_changed"]["total"] == 1
    assert classification["declared_cutovers"]["total"] == 1
    assert classification["refused"]["total"] == 0
    _assert_classification_reconciles(
        classification, previous_count=13, prospective_count=19
    )
    # T5 / C-E6: the fully-shaped published receipt also validates against
    # the JSON schema (Draft 2020-12) — the schema/runtime pair are the
    # same corpus.
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        receipt
    )


def test_receipt_validator_rejects_unsafe_classification_item_model_id(
    tmp_path: Path,
) -> None:
    """T4 / C-E5: injecting a model_id that violates the schema's
    ``^[A-Za-z0-9_.:-]+$`` regex must be rejected by ``_validate_receipt``
    with the schema-matching typed reason; the JSON schema also rejects
    the same shape (schema/runtime corpus stays aligned)."""
    receipt = refresh._receipt(
        run_id="refresh_refused",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_undeclared",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": "1" * 64,
            "new_registry_sha256": None,
            "added": {"items": [], "total": 0, "truncated": False},
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {
                "items": [
                    {
                        "model_id": "/etc/passwd",  # regex-invalid
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "refused": {
                "items": [
                    {
                        "model_id": "/etc/passwd",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "reason": "registry_cutover_undeclared",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError) as info:
        refresh._validate_receipt(receipt)
    assert "receipt_classification_invalid" in str(info.value)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(receipt)


def test_receipt_validator_rejects_bad_flat_classification_id(tmp_path: Path) -> None:
    """T4 continued: an ``added`` group model_id that violates the regex must
    also be rejected — this exercises ``_validate_registry_classification_field``
    directly (schema also rejects)."""
    receipt = refresh._receipt(
        run_id="refresh_refused_added",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="failed",
        reason="registry_cutover_undeclared",
        phase="precommit",
        providers=[],
        registry_classification={
            "previous_registry_sha256": "1" * 64,
            "new_registry_sha256": None,
            "added": {
                "items": ["basin/with/slash"],  # regex-invalid model_id
                "total": 1,
                "truncated": False,
            },
            "unchanged": {"items": [], "total": 0, "truncated": False},
            "removed": {"items": [], "total": 0, "truncated": False},
            "package_changed": {"items": [], "total": 0, "truncated": False},
            "refused": {
                "items": [
                    {
                        "model_id": "sane-id",
                        "old_checksum": None,
                        "new_checksum": "c" * 64,
                        "reason": "registry_cutover_undeclared",
                    }
                ],
                "total": 1,
                "truncated": False,
            },
            "declared_cutovers": {"items": [], "total": 0, "truncated": False},
        },
    )
    with pytest.raises(ValueError):
        refresh._validate_receipt(receipt)


def test_reconciliation_formula_helper_catches_missing_refused_entries(
    tmp_path: Path,
) -> None:
    """T3 / C-E4: the reconciliation helper itself catches a receipt whose
    ``refused`` bucket is smaller than ``removed + (package_changed \\
    declared_cutovers)`` — the property under test IS the formula, not any
    per-scenario hardcoded total."""
    bad_classification = {
        "previous_registry_sha256": "1" * 64,
        "new_registry_sha256": None,
        "added": {"items": [], "total": 0, "truncated": False},
        "unchanged": {"items": [], "total": 0, "truncated": False},
        "removed": {"items": ["basin-102"], "total": 1, "truncated": False},
        "package_changed": {"items": [], "total": 0, "truncated": False},
        "refused": {"items": [], "total": 0, "truncated": False},  # missing removal
        "declared_cutovers": {"items": [], "total": 0, "truncated": False},
    }
    with pytest.raises(AssertionError):
        _assert_classification_reconciles(
            bad_classification, previous_count=1, prospective_count=0
        )


def test_publish_primary_receipt_upgrades_over_pre_1080_latest(tmp_path: Path) -> None:
    """T9 / C-A2: a pre-#1080 ``latest.json`` (lacking
    ``registry_classification``) on disk must not brick the next refresh —
    ``_publish_primary_receipt`` reads it leniently and writes the new
    post-#1080 receipt over the top; ``validate_current_receipt`` (installer
    ``--enable``) then accepts the new receipt."""
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir(mode=0o700)
    # Legacy latest.json shape: pre-#1080 published receipt, no
    # registry_classification, otherwise valid.
    legacy_provider = {
        "name": "registry",
        "before_sha256": "1" * 64,
        "before_inode": 100,
        "before_schema_version": "nhms.scheduler.file_model_registry.v1",
        "before_generated_at": "2026-07-01T00:00:00Z",
        "before_payload_checksum": "sha256:" + "a" * 64,
        "after_sha256": "2" * 64,
        "after_schema_version": "nhms.scheduler.file_model_registry.v1",
        "after_generated_at": "2026-07-01T01:00:00Z",
        "after_payload_checksum": "sha256:" + "b" * 64,
        "entry_count": 13,
    }
    legacy_payload = {
        "schema_version": refresh.SCHEMA_VERSION,
        "run_id": "refresh_pre_1080",
        "started_at": "2026-07-01T00:00:00Z",
        "finished_at": "2026-07-01T00:05:00Z",
        "outcome": "published",
        "reason": "success",
        "operation_outcome": "published",
        "operation_reason": "success",
        "phase": "complete",
        "database_free": True,
        "providers": [
            legacy_provider,
            {**legacy_provider, "name": "readiness"},
            {**legacy_provider, "name": "state"},
        ],
        "orphans": {
            "items": [],
            "total": 0,
            "discovered_total": 0,
            "attempted_total": 0,
            "created_total": 0,
            "truncated": False,
        },
        "residues": [],
    }
    (receipt_root / "latest.json").write_bytes(
        json.dumps(legacy_payload, sort_keys=True, indent=2).encode() + b"\n"
    )
    # Now write a post-#1080 receipt via the real publisher.
    new_receipt = refresh._receipt(
        run_id="refresh_post_1080",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            {**legacy_provider, "name": "registry"},
            {**legacy_provider, "name": "readiness"},
            {**legacy_provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
    )
    # Would previously raise ValueError inside `_publish_primary_receipt`
    # because the stale latest.json fails `_validate_receipt`; must succeed.
    refresh._publish_primary_receipt(receipt_root, new_receipt)
    # Latest.json now holds the post-#1080 shape and validates strictly.
    persisted = json.loads((receipt_root / "latest.json").read_text())
    assert persisted["run_id"] == "refresh_post_1080"
    assert "registry_classification" in persisted
    refresh._validate_receipt(persisted)


def test_full_runner_over_pre_1080_latest_publishes_and_installer_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9 continued: the full runner completes ``outcome="published"`` when
    a pre-#1080 stub ``latest.json`` is present, and the new receipt is
    subsequently acceptable to ``validate_current_receipt``."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    # Seed the pre-#1080 latest.json.
    legacy_provider = {
        "name": "registry",
        "before_sha256": "1" * 64,
        "before_inode": 100,
        "before_schema_version": "nhms.scheduler.file_model_registry.v1",
        "before_generated_at": "2026-07-01T00:00:00Z",
        "before_payload_checksum": "sha256:" + "a" * 64,
        "after_sha256": "2" * 64,
        "after_schema_version": "nhms.scheduler.file_model_registry.v1",
        "after_generated_at": "2026-07-01T01:00:00Z",
        "after_payload_checksum": "sha256:" + "b" * 64,
        "entry_count": 1,
    }
    (config.receipt_root / "latest.json").write_bytes(
        json.dumps(
            {
                "schema_version": refresh.SCHEMA_VERSION,
                "run_id": "refresh_pre_1080",
                "started_at": "2026-07-01T00:00:00Z",
                "finished_at": "2026-07-01T00:05:00Z",
                "outcome": "published",
                "reason": "success",
                "operation_outcome": "published",
                "operation_reason": "success",
                "phase": "complete",
                "database_free": True,
                "providers": [
                    legacy_provider,
                    {**legacy_provider, "name": "readiness"},
                    {**legacy_provider, "name": "state"},
                ],
                "orphans": {
                    "items": [],
                    "total": 0,
                    "discovered_total": 0,
                    "attempted_total": 0,
                    "created_total": 0,
                    "truncated": False,
                },
                "residues": [],
            },
            sort_keys=True,
            indent=2,
        ).encode()
        + b"\n"
    )
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=previous_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published", receipt
    persisted = json.loads((config.receipt_root / "latest.json").read_text())
    assert persisted["run_id"] == receipt["run_id"]
    assert "registry_classification" in persisted


def test_full_runner_wall_clock_stable_generation_admits_deferred_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10 / C-B1: monkeypatch ``datetime.now(UTC)`` across a wall-clock
    boundary; a declaration filed at wall-clock T1 must still match the
    prospective generation at wall-clock T2 (which is exactly what the
    refuse -> declare -> retry operator loop requires)."""
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    prospective_models = [_registry_row("basin-101", "c" * 64)]

    # T1: refuse at 12:03:17.  We swap in a ``datetime`` proxy exposed as
    # ``refresh.datetime`` so ``refresh.datetime.now(UTC)`` returns whatever
    # the test's clock currently reads without touching the C-level
    # ``datetime.datetime`` type itself (which is not monkeypatchable).
    class _Clock:
        current = refresh.datetime(2026, 7, 14, 12, 3, 17, tzinfo=refresh.UTC)

    class _StubDateTime(refresh.datetime):  # type: ignore[misc]
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return _Clock.current

    monkeypatch.setattr(refresh, "datetime", _StubDateTime)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    first_receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert first_receipt["outcome"] == "failed"
    assert first_receipt["reason"] == "registry_cutover_undeclared"
    refused_generation = refresh._prospective_registry_generation(
        prospective_models, generated_at=_Clock.current
    )

    # Operator files a declaration bound to that generation.
    declaration = tmp_path / "declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": _Clock.current.isoformat().replace("+00:00", "Z"),
                "generation": refused_generation,
                "entries": [
                    {
                        "model_id": "basin-101",
                        "old_checksum": "a" * 64,
                        "new_checksum": "c" * 64,
                        "effective_cycle_utc": "2026-07-15T00:00:00Z",
                        "transition_mode": "replace",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    declaration.chmod(0o600)
    monkeypatch.setenv(refresh.CUTOVER_DECLARATION_ENV, str(declaration))

    # T2: retry 7 hours later; wall clock has advanced, but generation MUST
    # stay identical (finding C-B1).  Reset the pipeline stub because the
    # first refresh consumed it.
    _Clock.current = refresh.datetime(2026, 7, 14, 19, 42, 0, tzinfo=refresh.UTC)
    # Re-seed the previous canonical since the first refusal did not commit.
    _write_previous_canonical(config, previous_models)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    second_receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert second_receipt["outcome"] == "published", second_receipt
    classification = second_receipt["registry_classification"]
    assert classification["package_changed"]["total"] == 1
    assert classification["declared_cutovers"]["total"] == 1
    assert classification["refused"]["total"] == 0
    _assert_classification_reconciles(
        classification, previous_count=1, prospective_count=1
    )


def test_cutover_gate_escalates_source_inventory_checksum_drift(tmp_path: Path) -> None:
    """T11 / C-C1: two rows with identical URIs + package_checksum but a
    different nested ``resource_profile.source_inventory_checksum`` MUST
    classify as ``package_changed`` (spec.md:301-306).  Previously the
    classifier's 3-field whitelist silently classified them as unchanged."""
    config = _config(tmp_path)
    previous = [
        _registry_row(
            "basin-101",
            "a" * 64,
            source_inventory_checksum="sha256:" + "1" * 63 + "0",
        )
    ]
    prospective = [
        _registry_row(
            "basin-101",
            "a" * 64,  # top-level package_checksum unchanged
            source_inventory_checksum="sha256:" + "1" * 63 + "1",  # nested drift
        )
    ]

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    payload = captured[0]
    assert payload["unchanged"]["total"] == 0
    assert payload["package_changed"]["total"] == 1
    assert payload["package_changed"]["items"][0]["model_id"] == "basin-101"


def test_cutover_gate_escalates_basin_version_id_drift(tmp_path: Path) -> None:
    """T11 continued: a change to any top-level identity field
    (``basin_version_id`` here) escalates to ``package_changed``."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64, basin_version_id="v1")]
    prospective = [_registry_row("basin-101", "a" * 64, basin_version_id="v2")]

    captured, error = _run_gate(
        tmp_path, config, prospective_models=prospective, previous_models=previous
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    assert error.details["provider_reason"] == "registry_cutover_undeclared"
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1
    assert payload["unchanged"]["total"] == 0


def test_cutover_gate_escalates_missing_nested_identity(tmp_path: Path) -> None:
    """T11 continued: missing ``resource_profile`` on either side counts as
    identity drift — a rebuilt row that dropped the profile field must not
    ride through as ``unchanged``."""
    config = _config(tmp_path)
    previous = [_registry_row("basin-101", "a" * 64)]
    prospective_row = _registry_row("basin-101", "a" * 64)
    prospective_row.pop("resource_profile")

    captured, error = _run_gate(
        tmp_path,
        config,
        prospective_models=[prospective_row],
        previous_models=previous,
    )

    assert isinstance(error, refresh.SchedulerRegistryPublishError)
    payload = captured[0]
    assert payload["package_changed"]["total"] == 1


def test_load_previous_canonical_rejects_oversize_file(tmp_path: Path) -> None:
    """T (C-F1): explicit ``len > MAX`` sentinel after
    ``read_bytes_limited_no_follow`` in ``_load_previous_canonical_registry``."""
    manifest_dir = tmp_path / "registry"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "manifest-last.json"
    # Write MAX+1 bytes so read_bytes_limited_no_follow returns exactly
    # MAX+1 bytes (its sentinel-plus-one contract) and the caller's
    # explicit len > MAX check fires.
    manifest_path.write_bytes(b"x" * (refresh.MAX_REGISTRY_MANIFEST_BYTES + 1))
    assert manifest_path.stat().st_size > refresh.MAX_REGISTRY_MANIFEST_BYTES

    with pytest.raises(refresh.RefreshError) as info:
        refresh._load_previous_canonical_registry(
            str(manifest_path), containment_root=manifest_dir
        )
    assert info.value.reason == "provider_invalid"


def test_load_cutover_declaration_rejects_oversize_file(tmp_path: Path) -> None:
    """T (C-F4): explicit ``len > MAX`` sentinel in
    ``_load_cutover_declaration``."""
    declaration = tmp_path / "declaration.json"
    filler = b" " * (refresh.MAX_CUTOVER_DECLARATION_BYTES + 1)
    declaration.write_bytes(b"{}" + filler)
    with pytest.raises(refresh.RefreshError) as info:
        refresh._load_cutover_declaration(
            str(declaration), now=refresh.datetime.now(refresh.UTC)
        )
    assert info.value.reason == "registry_cutover_declaration_invalid"


def test_load_previous_canonical_returns_bytes_bound_to_sha(tmp_path: Path) -> None:
    """T (C-F2): the loader returns the exact bytes it hashed so callers
    can hand the snapshot forward without a second read (bytes+SHA must
    come from the same read)."""
    manifest_dir = tmp_path / "registry"
    manifest_dir.mkdir()
    manifest_path = manifest_dir / "manifest-last.json"
    manifest_path.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-14T00:00:00Z",
                "models": [_registry_row("basin-101", "a" * 64)],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    loaded = refresh._load_previous_canonical_registry(
        str(manifest_path), containment_root=manifest_dir
    )
    assert loaded is not None
    sha, models, raw_bytes = loaded
    assert sha == refresh.hashlib.sha256(raw_bytes).hexdigest()
    assert models[0]["model_id"] == "basin-101"


def test_full_runner_refresh_lock_is_held_during_precommit_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13 (part a) / C-E1: the precommit gate is invoked while
    ``config.refresh_lock`` is held by the runner.  A second attempt to
    acquire the same lock non-blocking must fail while the gate runs."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    _write_previous_canonical(config, previous_models)
    prospective_models = [_registry_row("basin-101", "a" * 64)]

    lock_holder_state: dict[str, bool] = {"seen_locked": False}

    # Monkeypatch the precommit gate implementation so we can observe that,
    # at the moment the gate runs, a competing non-blocking acquisition of
    # the same refresh_lock fails.  We do NOT replace the gate's semantics.
    original_gate = refresh._registry_precommit_gate

    def instrumented_gate(*args: object, **kwargs: object) -> None:
        # A second non-blocking acquire of the same refresh_lock must fail
        # with a typed ProviderAtomicError — proving the runner holds it.
        try:
            with provider_atomic_module.provider_destination_lock(
                config.refresh_lock, blocking=False
            ):
                # Successfully acquired means the runner did NOT hold it.
                lock_holder_state["seen_locked"] = False
        except provider_atomic_module.ProviderAtomicError:
            lock_holder_state["seen_locked"] = True
        return original_gate(*args, **kwargs)

    monkeypatch.setattr(refresh, "_registry_precommit_gate", instrumented_gate)
    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert receipt["outcome"] == "published", receipt
    assert lock_holder_state["seen_locked"] is True, (
        "refresh_lock was NOT held while the precommit gate ran — the "
        "concurrency invariant #1080 spec §D3/D7 relies on is broken."
    )


def test_full_runner_refusal_preserves_canonical_bytes_inode_and_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T13 (part c) / C-E1: a refused runner call leaves canonical bytes,
    inode, and mtime byte-identical (the receipt refusal contract)."""
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    config = _config(tmp_path)
    previous_models = [_registry_row("basin-101", "a" * 64)]
    canonical_path = _write_previous_canonical(config, previous_models)
    before_bytes = canonical_path.read_bytes()
    before_stat = canonical_path.stat()
    prospective_models = [_registry_row("basin-101", "c" * 64)]

    def publish_registry_with_drift(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](workspace, [], prospective_models)
        return {"selected_model_count": 1, "registry": None, "packages": []}

    _stub_provider_pipeline_with_models(
        monkeypatch, prospective_models=prospective_models
    )
    monkeypatch.setattr(
        refresh, "publish_all_basin_scheduler_registry", publish_registry_with_drift
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    assert canonical_path.read_bytes() == before_bytes
    after_stat = canonical_path.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )


def test_provider_atomic_cas_refuses_concurrent_authoritative_swap(
    tmp_path: Path,
) -> None:
    """T13 (part b) / C-E1: ``expected_preimage`` CAS prevents a concurrent
    canonical writer from committing after a snapshot.  This mirrors the
    invariant D3 relies on for the registry lane.
    """
    canonical = tmp_path / "registry" / "manifest-last.json"
    canonical.parent.mkdir()
    canonical.write_bytes(b"old\n")
    snapshot = capture_provider_preimage(canonical, max_bytes=1024)
    # Concurrent authoritative writer swaps the bytes.
    canonical.write_bytes(b"authoritative-new\n")

    with pytest.raises(ProviderAtomicError) as info:
        atomic_replace_provider_bytes(
            canonical,
            b"refresh-would-be\n",
            max_bytes=1024,
            expected_preimage=snapshot,
        )

    assert info.value.reason == "provider_preimage_changed"
    # Concurrent bytes preserved unchanged.
    assert canonical.read_bytes() == b"authoritative-new\n"


