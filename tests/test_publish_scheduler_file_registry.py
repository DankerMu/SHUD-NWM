from __future__ import annotations

import json
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    FileSchedulerModelRegistry,
    publish_canonical_readiness_index,
)
from workers.canonical_converter.converter import required_standard_variables_for_source
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed
from workers.model_registry.basins_soil_alpha_repair import repair_soil_alpha_calibration_for_basin


@pytest.fixture(autouse=True)
def _stub_source_identity_for_synthetic_inventories(monkeypatch: pytest.MonkeyPatch) -> None:
    real_source_identity = registry_script.basins_package_source_identity

    def source_identity(*, inventory_path: str | Path, model_id: str) -> dict[str, str]:
        inventory = _inventory_from_file(Path(inventory_path))
        model = next(
            (item for item in inventory.get("models", []) if item.get("model_id") == model_id),
            {},
        )
        required_files = model.get("required_files")
        if isinstance(required_files, dict) and len(required_files) > 10:
            return real_source_identity(inventory_path=inventory_path, model_id=model_id)
        return _source_identity(f"content:{model_id}", f"source:{model_id}")

    monkeypatch.setattr(
        registry_script,
        "basins_package_source_identity",
        source_identity,
    )


def _write_current_catalogs(object_root: Path) -> None:
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    for source_id in ("gfs", "IFS"):
        cycle = "2026071400"
        policy_identity = {"source": source_id}
        source_object_identity = {"manifest": f"raw/{source_id}/{cycle}/manifest.json"}
        products = []
        for variable in required_standard_variables_for_source(source_id):
            key = f"canonical/{source_id}/{cycle}/{variable}/f003.dat"
            content = f"{source_id}:{variable}:3".encode()
            store.write_bytes_atomic(key, content)
            products.append(
                {
                    "canonical_product_id": f"{source_id}_{cycle}_{variable}_f003",
                    "source_id": source_id,
                    "cycle_time": "2026-07-14T00:00:00Z",
                    "valid_time": "2026-07-14T03:00:00Z",
                    "lead_time_hours": 3,
                    "variable": variable,
                    "object_uri": store.uri_for_key(key),
                    "checksum": f"sha256:{sha256_bytes(content)}",
                    "quality_flag": "ok",
                    "lineage_json": {
                        "policy_identity": policy_identity,
                        "source_object_identity": source_object_identity,
                    },
                }
            )
        store.write_bytes_atomic(
            f"canonical/{source_id}/{cycle}/_catalog/catalog.json",
            json.dumps(
                {
                    "schema_version": "nhms.canonical.product_catalog.v1",
                    "source_id": source_id,
                    "cycle_time": "2026-07-14T00:00:00Z",
                    "products": products,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )


def test_package_version_for_nested_basin_is_safe_and_content_stable() -> None:
    model = _inventory_model("zhaochen/BST", shud_input_name="BST")
    identity = _source_identity("a", "b")

    first = registry_script.package_version_for_model(model, source_identity=identity)
    second = registry_script.package_version_for_model(dict(model), source_identity=dict(identity))

    assert first == second
    assert first.startswith("vbasins-zhaochen_bst-")
    assert "/" not in first


def test_package_version_is_stable_when_same_source_content_moves_workspace() -> None:
    old_model = _inventory_model("kashigeer")
    new_model = dict(old_model)
    new_model["source_path"] = "/volume/nwm/Basins/kashigeer"
    new_model["resolved_source_path"] = "/volume/nwm/Basins/kashigeer"
    new_model["input_dir"] = "/volume/nwm/Basins/kashigeer/input/kashigeer"

    identity = _source_identity("c", "d")
    assert registry_script.package_version_for_model(
        old_model,
        source_identity=identity,
    ) == registry_script.package_version_for_model(new_model, source_identity=identity)


def test_package_version_template_rejects_unsafe_path_segment() -> None:
    with pytest.raises(registry_script.SchedulerRegistryPublishError) as exc_info:
        registry_script.package_version_for_model(
            _inventory_model("qhh"),
            template="vbasins/{slug_id}",
            source_identity=_source_identity("e", "f"),
        )

    assert exc_info.value.error_code == "SCHEDULER_REGISTRY_PACKAGE_VERSION_UNSAFE"


def test_registry_context_limit_rejects_before_first_package_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basins = tmp_path / "Basins"
    basins.mkdir()
    inventory = {"model_count": 4097, "models": []}
    selected = [{"model_id": f"model-{index}"} for index in range(4097)]
    package_calls = 0

    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "_select_publishable_models", lambda *args, **kwargs: selected)

    def count_package(**kwargs: object) -> dict[str, Any]:
        nonlocal package_calls
        del kwargs
        package_calls += 1
        return {}

    monkeypatch.setattr(registry_script, "publish_basins_package", count_package)

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as error_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=basins,
            registry_manifest=tmp_path / "provider" / "manifest.json",
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work",
            repair_missing_radiation=False,
            max_contexts=4096,
        )

    assert package_calls == 0
    assert error_info.value.details["context_total"] == 4097
    assert error_info.value.details["created_total"] == 0


def test_context_two_import_failure_reports_all_new_packages_and_preserves_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [_inventory_model("first"), _inventory_model("second")]
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 2,
        "models": models,
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    imports = 0

    def fail_second_import(inventory_path: str | Path, package_manifest_path: str | Path) -> SimpleNamespace:
        nonlocal imports
        imports += 1
        if imports == 2:
            raise RuntimeError(f"private path must be sanitized: {package_manifest_path}")
        return _fake_sources(inventory, Path(package_manifest_path))

    monkeypatch.setattr(registry_script, "prepare_basins_import_sources", fail_second_import)
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"canonical-before")
    before = canonical.read_bytes()

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as error_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=tmp_path / "Basins",
            registry_manifest=canonical,
            object_store_root=tmp_path / "private-objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work",
        )

    details = error_info.value.details
    assert details["discovered_total"] == 2
    assert details["attempted_total"] == 2
    assert details["created_total"] == 2
    assert len(details["packages"]) == 2
    assert str(tmp_path) not in json.dumps(details)
    assert canonical.read_bytes() == before


def test_completed_import_sources_are_released_before_preparing_next_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [_inventory_model("first"), _inventory_model("second")]
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 2,
        "models": models,
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)

    class WeakSources:
        pass

    previous_sources: weakref.ReferenceType[WeakSources] | None = None

    def prepare(inventory_path: str | Path, package_manifest_path: str | Path) -> WeakSources:
        nonlocal previous_sources
        if previous_sources is not None:
            assert previous_sources() is None
        prepared = _fake_sources(_inventory_from_file(Path(inventory_path)), Path(package_manifest_path))
        sources = WeakSources()
        vars(sources).update(vars(prepared))
        previous_sources = weakref.ref(sources)
        return sources

    monkeypatch.setattr(registry_script, "prepare_basins_import_sources", prepare)
    monkeypatch.setattr(
        registry_script,
        "scheduler_registry_row_from_sources",
        lambda sources, **_kwargs: {"model_id": sources.ids["model_id"]},
    )
    monkeypatch.setattr(
        registry_script,
        "publish_scheduler_registry_manifest",
        lambda *_args, **_kwargs: {"model_count": 2},
    )

    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=tmp_path / "Basins",
        registry_manifest=tmp_path / "objects" / "scheduler" / "registry" / "manifest-last.json",
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
    )

    assert summary["selected_model_count"] == 2
    assert previous_sources is not None
    assert previous_sources() is None


def test_failed_package_after_immutable_manifest_is_counted_as_new_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _inventory_model("first")
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [model],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)

    def commit_then_fail(**kwargs: Any) -> dict[str, Any]:
        model_id = str(kwargs["model_id"])
        version = str(kwargs["version"])
        kwargs["object_store"].write_bytes_atomic(
            f"models/{model_id}/{version}/manifest.json",
            b"{}\n",
        )
        raise RuntimeError("late local failure")

    monkeypatch.setattr(registry_script, "publish_basins_package", commit_then_fail)
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"canonical-before")

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as error_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=tmp_path / "Basins",
            registry_manifest=canonical,
            object_store_root=tmp_path / "private-objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work",
        )

    assert error_info.value.details["attempted_total"] == 1
    assert error_info.value.details["created_total"] == 1
    assert len(error_info.value.details["packages"]) == 1
    assert canonical.read_bytes() == b"canonical-before"


def test_context_two_resource_failure_reports_only_prior_published_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [_inventory_model("first"), _inventory_model("second")]
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 2,
        "models": models,
        "warnings": [],
    }
    imported = 0
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)

    def prepare(inventory_path: str | Path, package_manifest_path: str | Path) -> SimpleNamespace:
        nonlocal imported
        imported += 1
        return _fake_sources(inventory, Path(package_manifest_path))

    def resource_guard(_workspace: Path) -> None:
        if imported == 1:
            raise refresh.RefreshError("workspace_limit_exceeded")

    monkeypatch.setattr(registry_script, "prepare_basins_import_sources", prepare)
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"canonical-before")

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as error_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=tmp_path / "Basins",
            registry_manifest=canonical,
            object_store_root=tmp_path / "private-objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work",
            resource_validator=resource_guard,
        )

    assert error_info.value.details["discovered_total"] == 2
    assert error_info.value.details["attempted_total"] == 2
    assert error_info.value.details["created_total"] == 1
    assert error_info.value.details["provider_reason"] == "workspace_limit_exceeded"
    assert canonical.read_bytes() == b"canonical-before"


def test_canonical_preimage_failure_reports_all_new_packages_and_preserves_authoritative_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [_inventory_model("first"), _inventory_model("second")]
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 2,
        "models": models,
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(inventory, Path(package_manifest_path)),
    )
    private_root = tmp_path / "private-objects"
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    first = registry_script.publish_scheduler_registry_manifest(
        [],
        canonical,
        object_store_root=private_root,
        object_store_prefix="s3://nhms",
        generated_at=registry_script.datetime(2026, 7, 14, tzinfo=registry_script.UTC),
    )
    stale = registry_script.ProviderPreimage(
        exists=True,
        sha256=str(first["content_sha256"]),
        device=canonical.stat().st_dev,
        inode=canonical.stat().st_ino,
        mode=canonical.stat().st_mode & 0o777,
        uid=canonical.stat().st_uid,
        gid=canonical.stat().st_gid,
        size=canonical.stat().st_size,
        mtime_ns=canonical.stat().st_mtime_ns,
    )
    registry_script.publish_scheduler_registry_manifest(
        [],
        canonical,
        object_store_root=private_root,
        object_store_prefix="s3://nhms",
        generated_at=registry_script.datetime(2026, 7, 14, 1, tzinfo=registry_script.UTC),
    )
    authoritative = canonical.read_bytes()

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as error_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=tmp_path / "Basins",
            registry_manifest=canonical,
            object_store_root=private_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work",
            expected_preimage=stale,
        )

    assert error_info.value.details["provider_reason"] == "provider_preimage_changed"
    assert error_info.value.details["attempted_total"] == 2
    assert error_info.value.details["created_total"] == 2
    assert canonical.read_bytes() == authoritative


def test_orphan_sample_filters_published_before_first_256_slice() -> None:
    results = [
        {
            "status": "published" if index % 2 else "already_done",
            "manifest_uri": f"s3://nhms/models/model-{index}/v1/manifest.json",
        }
        for index in range(700)
    ]

    error = registry_script._publish_failure(
        RuntimeError("failed"),
        discovered_total=700,
        attempted_total=700,
        package_results=results,
        error_code="TEST",
        message="sanitized",
    )

    assert error.details["created_total"] == 350
    assert len(error.details["packages"]) == 256
    expected_last = registry_script.hashlib.sha256(
        b"s3://nhms/models/model-511/v1/manifest.json"
    ).hexdigest()[:32]
    assert error.details["packages"][-1]["orphan_id"] == expected_last


def test_publish_all_basin_scheduler_registry_writes_all_publishable_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 3,
        "models": [
            _inventory_model("qhh"),
            _inventory_model("zhaochen/BST", shud_input_name="BST"),
            {
                **_inventory_model("bad"),
                "status": "partial",
                "default_publish_eligible": False,
                "missing_required_files": ["*.tsd.rl"],
            },
        ],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory,
            Path(package_manifest_path),
        ),
    )

    object_root = tmp_path / "object-store"
    registry_manifest = object_root / "scheduler" / "registry" / "manifest-last.json"
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=tmp_path / "Basins",
        registry_manifest=registry_manifest,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
    )

    assert summary["status"] == "published"
    assert summary["discovered_model_count"] == 3
    assert summary["selected_model_count"] == 2
    assert summary["selected_basin_slugs"] == ["qhh", "zhaochen/BST"]
    assert summary["package_status_counts"] == {"published": 2}
    payload = json.loads(registry_manifest.read_text(encoding="utf-8"))
    rows = {row["model_id"]: row for row in payload["models"]}
    assert set(rows) == {"basins_qhh_shud", "basins_zhaochen_bst_shud"}
    assert rows["basins_qhh_shud"]["display_capabilities"] == {"q_down": True, "tiles": True}
    assert rows["basins_qhh_shud"]["resource_profile"]["lineage"] == "basins_scheduler_file_registry"
    assert rows["basins_zhaochen_bst_shud"]["resource_profile"]["project_name"] == "BST"
    assert rows["basins_zhaochen_bst_shud"]["output_segment_count"] == 7


def test_registry_precommit_receives_same_generation_identities_before_manifest_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 2,
        "models": [_inventory_model("first"), _inventory_model("second")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(inventory, Path(package_manifest_path)),
    )
    destination = tmp_path / "shared/scheduler/registry/manifest-last.json"
    observed: dict[str, object] = {}

    def precommit(
        workspace: Path,
        packages: list[dict[str, Any]],
        registry_models: list[dict[str, Any]],
    ) -> None:
        observed["workspace_exists"] = workspace.is_dir()
        observed["package_count"] = len(packages)
        observed["model_pairs"] = {
            (str(model["model_id"]), str(model["basin_id"])) for model in registry_models
        }
        observed["destination_exists"] = destination.exists()

    registry_script.publish_all_basin_scheduler_registry(
        basins_root=tmp_path / "Basins",
        registry_manifest=destination,
        object_store_root=tmp_path / "private-objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
        precommit_validator=precommit,
    )

    assert observed == {
        "workspace_exists": True,
        "package_count": 2,
        "model_pairs": {
            ("basins_first_shud", "basins_first"),
            ("basins_second_shud", "basins_second"),
        },
        "destination_exists": False,
    }
    assert destination.is_file()


def test_real_registry_refresh_keeps_packages_private_and_canonical_manifest_shared(
    tmp_path: Path,
) -> None:
    from tests.test_basins_registry_import import _write_registry_fixture

    basins_root, _input_dir, _inventory_path, _manifest_path, model_id = (
        _write_registry_fixture(tmp_path / "fixture")
    )
    private_objects = tmp_path / "private-objects"
    shared_providers = tmp_path / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"

    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
        repair_missing_radiation=False,
    )

    assert summary["status"] == "published"
    assert registry_manifest.is_file()
    private_manifest = Path(
        private_objects,
        summary["packages"][0]["manifest_uri"].removeprefix("s3://nhms/"),
    )
    assert private_manifest.is_file()
    assert not (shared_providers / "models").exists()
    readiness = shared_providers / "scheduler/canonical-readiness/index-last.json"
    state = shared_providers / "scheduler/state-index/index-last.json"
    publish_canonical_readiness_index(
        [],
        readiness,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
    )
    publish_state_snapshot_index(
        [],
        state,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
    )
    _write_current_catalogs(private_objects)
    runtime = tmp_path / "runtime"
    work = runtime / "work"
    receipts = runtime / "receipts"
    emergency = runtime / "emergency"
    for directory in (runtime, work, receipts, emergency):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
    receipt = refresh.refresh_scheduler_file_providers(
        refresh.RefreshConfig(
            basins_root=basins_root,
            registry_uri=str(registry_manifest),
            readiness_uri=str(readiness),
            state_uri=str(state),
            object_store_root=private_objects,
            provider_store_root=shared_providers,
            object_store_prefix="s3://nhms",
            workspace_root=work,
            receipt_root=receipts,
            emergency_root=emergency,
            refresh_lock=runtime / "refresh.lock",
        ),
        dry_run=False,
    )
    assert receipt["outcome"] == "published", receipt
    assert [provider["name"] for provider in receipt["providers"]] == [
        "registry",
        "readiness",
        "state",
    ]
    assert not (shared_providers / "models").exists()
    registry = FileSchedulerModelRegistry(
        registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        now=registry_script.datetime.now(registry_script.UTC),
    )
    assert registry.list_models(basin_version_id=None, active=True, limit=10, offset=0)["total"] == 1
    assert registry.get_model(model_id)["model_id"] == model_id

    private_manifest.unlink()
    missing = FileSchedulerModelRegistry(
        registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        now=registry_script.datetime.now(registry_script.UTC),
    )
    assert missing.list_models(basin_version_id=None, active=True, limit=10, offset=0)["items"] == []
    assert missing.scheduler_registry_evidence()["blockers"][0]["code"] == (
        "registry_model_package_manifest_missing"
    )


def test_refresh_inventory_fixture_publishes_exact_thirteen_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [_inventory_model(f"basin-{index:02d}") for index in range(13)]
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": len(models),
        "models": models,
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(inventory, Path(package_manifest_path)),
    )

    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=tmp_path / "Basins",
        registry_manifest=tmp_path / "objects/scheduler/registry/manifest-last.json",
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
    )

    assert summary["selected_model_count"] == 13
    assert summary["registry"]["model_count"] == 13
    assert summary["package_status_counts"] == {"published": 13}


def test_missing_radiation_repair_copies_matching_template_inside_private_root(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    target_input = isolated / "tailanhe" / "input" / "tlh"
    target_input.mkdir(parents=True)
    (target_input / "tlh.tsd.lai").write_text("900\t18\t19810101\t20551201\t86400\nlai\n", encoding="utf-8")
    template = tmp_path / "Basins" / "heihe" / "input" / "heihe" / "heihe.tsd.rl"
    template.parent.mkdir(parents=True)
    template.write_text("900\t18\t19810101\t20551201\t86400\nradiation\n", encoding="utf-8")

    report = repair_missing_tsd_rl_for_basin(
        isolated_root=isolated,
        basin_slug="tailanhe",
        template_search_root=tmp_path / "Basins",
    )

    assert repair_performed(report)
    assert (target_input / "tlh.tsd.rl").read_text(encoding="utf-8") == template.read_text(encoding="utf-8")
    assert report["repairs"][0]["template"] == str(template)


def test_missing_radiation_repair_budget_rejects_before_target_creation(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    target_input = isolated / "tailanhe" / "input" / "tlh"
    target_input.mkdir(parents=True)
    lai = target_input / "tlh.tsd.lai"
    lai.write_text("900\t18\t19810101\t20551201\t86400\nlai\n", encoding="utf-8")
    template = tmp_path / "templates" / "heihe.tsd.rl"
    template.parent.mkdir()
    template.write_text("900\t18\t19810101\t20551201\t86400\nradiation\n", encoding="utf-8")
    budget = refresh._WorkspaceBudget(
        isolated,
        max_bytes=lai.stat().st_size,
        max_entries=32,
        max_depth=8,
    )

    with pytest.raises(refresh.RefreshError, match="workspace_limit_exceeded"):
        repair_missing_tsd_rl_for_basin(
            isolated_root=isolated,
            basin_slug="tailanhe",
            template_search_root=template.parent,
            copy_file=budget.copy_file,
        )

    assert not (target_input / "tlh.tsd.rl").exists()


def test_publish_all_basin_scheduler_registry_repairs_missing_radiation_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basins_root = tmp_path / "Basins"
    tailanhe_input = basins_root / "tailanhe" / "input" / "tlh"
    tailanhe_input.mkdir(parents=True)
    (tailanhe_input / "tlh.tsd.lai").write_text("900\t18\t19810101\t20551201\t86400\nlai\n", encoding="utf-8")
    template = basins_root / "heihe" / "input" / "heihe" / "heihe.tsd.rl"
    template.parent.mkdir(parents=True)
    template.write_text("900\t18\t19810101\t20551201\t86400\nradiation\n", encoding="utf-8")
    initial_inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(basins_root),
        "resolved_root": str(basins_root),
        "model_count": 2,
        "models": [
            _inventory_model("qhh"),
            {
                **_inventory_model("tailanhe", shud_input_name="tlh"),
                "source_path": str(basins_root / "tailanhe"),
                "resolved_source_path": str(basins_root / "tailanhe"),
                "input_dir": str(tailanhe_input),
                "status": "partial",
                "default_publish_eligible": False,
                "missing_required_files": ["*.tsd.rl"],
            },
        ],
        "warnings": [],
    }

    def fake_discover(root: Path) -> dict[str, Any]:
        if Path(root) == basins_root:
            return initial_inventory
        repaired = _inventory_model("tailanhe", shud_input_name="tlh")
        repaired["source_path"] = str(Path(root) / "tailanhe")
        repaired["resolved_source_path"] = str(Path(root) / "tailanhe")
        repaired["input_dir"] = str(Path(root) / "tailanhe" / "input" / "tlh")
        return {
            "schema_version": "basins.discovery.v1",
            "root": str(root),
            "resolved_root": str(root),
            "model_count": 1,
            "models": [repaired],
            "warnings": [],
        }

    monkeypatch.setattr(registry_script, "discover_basins_inventory", fake_discover)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            _inventory_from_file(Path(inventory_path)),
            Path(package_manifest_path),
        ),
    )

    object_root = tmp_path / "object-store"
    registry_manifest = object_root / "scheduler" / "registry" / "manifest-last.json"
    run_workspace = tmp_path / "run-workspace"
    run_workspace.mkdir()
    work_dir = run_workspace / "registry"
    workspace_budget = refresh._WorkspaceBudget(
        run_workspace,
        max_bytes=32 * 1024 * 1024,
        max_entries=1024,
        max_depth=16,
    )
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
        resource_validator=refresh._enforce_workspace_bounds,
        workspace_budget=workspace_budget,
    )

    assert summary["selected_basin_slugs"] == ["qhh", "tailanhe"]
    assert len(summary["repairs"]) == 1
    assert summary["repairs"][0]["basin_slug"] == "tailanhe"
    assert summary["repair_staging_cleanup"]["status"] == "cleaned"
    assert summary["repair_staging_cleanup"]["removed"][0]["name"] == "repaired-basins"
    assert not (work_dir / "repaired-basins").exists()
    payload = json.loads(registry_manifest.read_text(encoding="utf-8"))
    assert {row["model_id"] for row in payload["models"]} == {"basins_qhh_shud", "basins_tailanhe_shud"}


def test_soil_alpha_repair_reduces_calibrated_multiplier_inside_private_root(tmp_path: Path) -> None:
    input_dir = _write_soil_alpha_model_files(tmp_path / "isolated", "hetianhe", "hetian9000-2")

    dry_run = repair_soil_alpha_calibration_for_basin(
        isolated_root=tmp_path / "isolated",
        basin_slug="hetianhe",
        dry_run=True,
    )
    assert dry_run["repairs"][0]["status"] == "would_repair"
    assert "SOIL_ALPHA\t8.19327372615961" in (input_dir / "hetian9000-2.cfg.calib").read_text(encoding="utf-8")

    report = repair_soil_alpha_calibration_for_basin(
        isolated_root=tmp_path / "isolated",
        basin_slug="hetianhe",
    )

    repair = report["repairs"][0]
    assert repair["status"] == "repaired"
    assert repair["soil_alpha_multiplier_before"] == pytest.approx(8.19327372615961)
    assert repair["soil_alpha_multiplier_after"] == pytest.approx(19.999 / 6.380619)
    assert repair["calibrated_alpha_max_after"] <= 20.0
    assert "SOIL_ALPHA\t8.19327372615961" not in (input_dir / "hetian9000-2.cfg.calib").read_text(
        encoding="utf-8"
    )


def test_soil_alpha_repair_budget_rejects_before_cfg_mutation(tmp_path: Path) -> None:
    isolated = tmp_path / "isolated"
    input_dir = _write_soil_alpha_model_files(isolated, "hetianhe", "hetian9000-2")
    cfg = input_dir / "hetian9000-2.cfg.calib"
    original = "GEOL_KSATH\t0.009\nSOIL_ALPHA\t9\nRIV_ROUGH\t0.2\n"
    cfg.write_text(original, encoding="utf-8")
    initial_bytes = sum(path.stat().st_size for path in isolated.rglob("*") if path.is_file())
    budget = refresh._WorkspaceBudget(
        isolated,
        max_bytes=initial_bytes,
        max_entries=32,
        max_depth=8,
    )

    with pytest.raises(refresh.RefreshError, match="workspace_limit_exceeded"):
        repair_soil_alpha_calibration_for_basin(
            isolated_root=isolated,
            basin_slug="hetianhe",
            write_bytes=budget.write_bytes,
        )

    assert cfg.read_text(encoding="utf-8") == original


def test_geol_dmac_repair_reduces_calibrated_depth_inside_private_root(tmp_path: Path) -> None:
    input_dir = _write_geol_dmac_model_files(tmp_path / "isolated", "hetianhe", "hetian9000-2")

    dry_run = repair_soil_alpha_calibration_for_basin(
        isolated_root=tmp_path / "isolated",
        basin_slug="hetianhe",
        dry_run=True,
    )
    assert dry_run["repairs"][0]["status"] == "would_repair"
    assert "GEOL_DMAC\t5" in (input_dir / "hetian9000-2.cfg.calib").read_text(encoding="utf-8")

    report = repair_soil_alpha_calibration_for_basin(
        isolated_root=tmp_path / "isolated",
        basin_slug="hetianhe",
    )

    repair = report["repairs"][0]
    assert repair["status"] == "repaired"
    assert repair["parameter"] == "GEOL_DMAC"
    assert repair["geol_dmac_multiplier_before"] == pytest.approx(5)
    assert repair["geol_dmac_multiplier_after"] == pytest.approx(4)
    assert repair["calibrated_dmac_max_after"] <= 4.0
    assert "GEOL_DMAC\t5" not in (input_dir / "hetian9000-2.cfg.calib").read_text(encoding="utf-8")


def test_publish_all_basin_scheduler_registry_repairs_calibrated_soil_alpha_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basins_root = tmp_path / "Basins"
    _write_soil_alpha_model_files(basins_root, "hetianhe", "hetian9000-2")
    initial_model = _inventory_model("hetianhe", shud_input_name="hetian9000-2")
    initial_model["source_path"] = str(basins_root / "hetianhe")
    initial_model["resolved_source_path"] = str(basins_root / "hetianhe")
    initial_model["input_dir"] = str(basins_root / "hetianhe" / "input" / "hetian9000-2")
    initial_inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(basins_root),
        "resolved_root": str(basins_root),
        "model_count": 1,
        "models": [initial_model],
        "warnings": [],
    }

    def fake_discover(root: Path) -> dict[str, Any]:
        if Path(root) == basins_root:
            return initial_inventory
        repaired = _inventory_model("hetianhe", shud_input_name="hetian9000-2")
        repaired["source_path"] = str(Path(root) / "hetianhe")
        repaired["resolved_source_path"] = str(Path(root) / "hetianhe")
        repaired["input_dir"] = str(Path(root) / "hetianhe" / "input" / "hetian9000-2")
        repaired["checksums"] = {"hetian9000-2.cfg.calib": "repaired-sha"}
        return {
            "schema_version": "basins.discovery.v1",
            "root": str(root),
            "resolved_root": str(root),
            "model_count": 1,
            "models": [repaired],
            "warnings": [],
        }

    monkeypatch.setattr(registry_script, "discover_basins_inventory", fake_discover)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            _inventory_from_file(Path(inventory_path)),
            Path(package_manifest_path),
        ),
    )

    object_root = tmp_path / "object-store"
    registry_manifest = object_root / "scheduler" / "registry" / "manifest-last.json"
    run_workspace = tmp_path / "run-workspace"
    run_workspace.mkdir()
    work_dir = run_workspace / "registry"
    workspace_budget = refresh._WorkspaceBudget(
        run_workspace,
        max_bytes=32 * 1024 * 1024,
        max_entries=1024,
        max_depth=16,
    )
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
        retain_repair_staging=True,
        resource_validator=refresh._enforce_workspace_bounds,
        workspace_budget=workspace_budget,
    )

    assert summary["selected_basin_slugs"] == ["hetianhe"]
    assert summary["repair_staging_cleanup"] == {"status": "retained", "reason": "retain_repair_staging"}
    assert len(summary["repairs"]) == 1
    assert summary["repairs"][0]["schema_version"] == "basins.calibration_repair.v1"
    repair = summary["repairs"][0]["repairs"][0]
    assert repair["soil_alpha_multiplier_after"] == pytest.approx(19.999 / 6.380619)
    repaired_cfg = Path(repair["cfg_calib"])
    assert repaired_cfg.is_file()
    assert "SOIL_ALPHA\t8.19327372615961" not in repaired_cfg.read_text(encoding="utf-8")
    payload = json.loads(registry_manifest.read_text(encoding="utf-8"))
    assert {row["model_id"] for row in payload["models"]} == {"basins_hetianhe_shud"}


def _inventory_model(basin_slug: str, *, shud_input_name: str | None = None) -> dict[str, Any]:
    slug_id = registry_script._slug_id(basin_slug)
    input_name = shud_input_name or basin_slug.rsplit("/", maxsplit=1)[-1]
    return {
        "basin_slug": basin_slug,
        "source_path": f"/Basins/{basin_slug}",
        "resolved_source_path": f"/Basins/{basin_slug}",
        "source_is_symlink": False,
        "shud_input_name": input_name,
        "input_dir": f"/Basins/{basin_slug}/input/{input_name}",
        "status": "valid",
        "model_id": f"basins_{slug_id}_shud",
        "suggested_ids": {
            "basin_id": f"basins_{slug_id}",
            "basin_version_id": f"basins_{slug_id}_vbasins",
            "river_network_version_id": f"basins_{slug_id}_rivnet_vbasins",
            "mesh_version_id": f"basins_{slug_id}_mesh_vbasins",
            "model_id": f"basins_{slug_id}_shud",
        },
        "required_files": {"cfg_para": [f"{input_name}.cfg.para"]},
        "checksums": {f"{input_name}.cfg.para": f"sha-{slug_id}"},
        "default_import_eligible": True,
        "default_publish_eligible": True,
        "root_relative_path": basin_slug,
        "root_relative_resolved_path": basin_slug,
    }


def _source_identity(content_seed: str, source_seed: str) -> dict[str, str]:
    return {
        "schema_version": "basins.package.source_identity.v1",
        "content_sha256": sha256_bytes(content_seed.encode("utf-8")),
        "source_sha256": sha256_bytes(source_seed.encode("utf-8")),
    }


def _fake_publish_basins_package(
    *,
    inventory_path: str | Path,
    model_id: str,
    version: str,
    output_path: str | Path,
    copy_forcing: bool,
    object_store: Any,
    output_capacity_guard: Any = None,
    output_write_guard: Any = None,
    expected_source_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del inventory_path, copy_forcing, expected_source_identity
    manifest_key = f"models/{model_id}/{version}/manifest.json"
    manifest_uri = object_store.uri_for_key(manifest_key)
    manifest = {
        "schema_version": "basins.package.v1",
        "model_id": model_id,
        "version": version,
        "basin_slug": model_id.removeprefix("basins_").removesuffix("_shud"),
        "shud_input_name": model_id,
        "model_package_uri": f"s3://nhms/models/{model_id}/{version}/package/",
        "manifest_uri": manifest_uri,
        "package_checksum": f"package-sha-{model_id}",
        "source_inventory_checksum": "inventory-sha",
        "source_inventory_schema_version": "basins.discovery.v1",
        "included_files": [],
    }
    content = json.dumps(manifest, sort_keys=True).encode("utf-8")
    output = Path(output_path)
    if output_capacity_guard is not None:
        output_capacity_guard(output, 16 * 1024 * 1024)
    if output_write_guard is not None:
        output_write_guard(output, len(content))
    object_store.write_bytes_atomic(manifest_key, content)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return {
        "status": "published",
        "model_id": model_id,
        "version": version,
        "model_package_uri": manifest["model_package_uri"],
        "manifest_uri": manifest_uri,
        "package_checksum": manifest["package_checksum"],
    }


def _fake_sources(inventory: dict[str, Any], package_manifest_path: Path) -> SimpleNamespace:
    manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    model = next(model for model in inventory["models"] if model["model_id"] == manifest["model_id"])
    return SimpleNamespace(
        model=model,
        manifest=manifest,
        ids=model["suggested_ids"],
        geometry=SimpleNamespace(
            segment_count=11,
            output_segment_count=7,
            evidence_counts={"river_count": 7, "rivseg_segment_count": 11},
        ),
    )


def _inventory_from_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_soil_alpha_model_files(root: Path, basin_slug: str, input_name: str) -> Path:
    input_dir = root / basin_slug / "input" / input_name
    input_dir.mkdir(parents=True)
    (input_dir / f"{input_name}.cfg.calib").write_text(
        "GEOL_KSATH\t0.00977999747288218\n"
        "SOIL_ALPHA\t8.19327372615961\n"
        "RIV_ROUGH\t0.2\n",
        encoding="utf-8",
    )
    (input_dir / f"{input_name}.para.soil").write_text(
        "3\t9\n"
        "INDEX\tKsatV(m_d)\tThetaS(m3_m3)\tThetaR(m3_m3)\tInfD(m)\tAlpha(1_m)\tBeta\thAreaF(m2_m2)\tmacKsatV(m_d)\n"
        "1\t0.3066345\t0.4369851\t0.01\t0.1\t3.141588\t1.228055\t0.01\t30.66345\n"
        "2\t0.412565\t0.4509599\t0.01\t0.1\t6.380619\t1.220865\t0.01\t41.2565\n"
        "3\t0.493972\t0.4669714\t0.01\t0.1\t4.640145\t1.217887\t0.01\t49.3972\n",
        encoding="utf-8",
    )
    return input_dir


def _write_geol_dmac_model_files(root: Path, basin_slug: str, input_name: str) -> Path:
    input_dir = root / basin_slug / "input" / input_name
    input_dir.mkdir(parents=True)
    (input_dir / f"{input_name}.cfg.calib").write_text(
        "GEOL_KSATH\t0.00977999747288218\n"
        "GEOL_DMAC\t5\n"
        "SOIL_ALPHA\t1\n",
        encoding="utf-8",
    )
    (input_dir / f"{input_name}.para.geol").write_text(
        "3\t8\n"
        "INDEX\tKsatH(m_d)\tKsatV(m_d)\tThetaS(m3_m3)\tThetaR(m3_m3)\tvAreaF(m2_m2)\tmacKsatH(m_d)\tDmac(m)\n"
        "1\t0.9441873\t0.09441873\t0.3889031\t0.01\t0.01\t94.41873\t1\n"
        "2\t3.049162\t0.3049162\t0.4479848\t0.01\t0.01\t304.9162\t1\n"
        "3\t3.568563\t0.3568563\t0.4556972\t0.01\t0.01\t356.8563\t1\n",
        encoding="utf-8",
    )
    return input_dir
