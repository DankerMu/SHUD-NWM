from __future__ import annotations

import json
import weakref
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
import workers.model_registry.basins_calibration_overrides as basins_calibration_overrides
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    FileSchedulerModelRegistry,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination
from workers.canonical_converter.converter import required_standard_variables_for_source
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed

# #1832 round-2 C2: a declared basin that the discovered inventory does not
# contain is now a refusal, and the checked-in declaration names `hetianhe`.
# The suites below publish synthetic fixture trees that contain no such basin
# and are not about calibration overrides at all, so they take the module's
# documented escape hatch and load no declaration.  Default loading is pinned
# where it belongs: `test_checked_in_declaration_loads_without_anyone_naming_it`
# and the two refresh-lane receipt tests.
_NO_DECLARATION: Path | None = None


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
            calibration_overrides_path=_NO_DECLARATION,
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
            calibration_overrides_path=_NO_DECLARATION,
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
        calibration_overrides_path=_NO_DECLARATION,
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
            calibration_overrides_path=_NO_DECLARATION,
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
            calibration_overrides_path=_NO_DECLARATION,
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
            calibration_overrides_path=_NO_DECLARATION,
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
        calibration_overrides_path=_NO_DECLARATION,
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
        calibration_overrides_path=_NO_DECLARATION,
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_basins_registry_import import _write_registry_fixture

    basins_root, _input_dir, _inventory_path, _manifest_path, model_id = (
        _write_registry_fixture(tmp_path / "fixture")
    )
    private_objects = tmp_path / "private-objects"
    shared_providers = tmp_path / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"

    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
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
    # #1097 / spec scenario 2: capture the runner-built audit block on its way
    # into the publisher and the receipt dict handed back on its way out, so
    # the assertion below compares the producer's block against what the real
    # publisher returned (call-through spy — the real publisher runs). On this
    # runner channel the returned block is the only evidence: the runner's own
    # receipt drops cutover_gate via _provider_evidence, so nothing persists it
    # here; CLI-channel persistence is covered by a separate test.
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    real_publish_all = refresh.publish_all_basin_scheduler_registry
    passthrough: dict[str, Any] = {}

    def _spy_publish_all(**kwargs: Any) -> dict[str, Any]:
        passthrough["producer_block"] = kwargs.get("cutover_gate")
        summary = real_publish_all(**kwargs)
        passthrough["summary"] = summary
        return summary

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", _spy_publish_all)
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
            calibration_overrides_path=_NO_DECLARATION,
        ),
        dry_run=False,
    )
    assert receipt["outcome"] == "published", receipt
    assert [provider["name"] for provider in receipt["providers"]] == [
        "registry",
        "readiness",
        "state",
    ]
    # #1097 / spec scenario 2: the runner's enforced audit block survives into
    # the manifest companion receipt byte-for-byte — no field dropped, no mode
    # rewritten between producer and persisted receipt.
    producer_block = passthrough["producer_block"]
    assert producer_block == {
        "mode": "enforced",
        "declaration_env": refresh.CUTOVER_DECLARATION_ENV,
        "declaration_present": False,
    }, producer_block
    receipt_block = passthrough["summary"]["registry"]["cutover_gate"]
    assert receipt_block == producer_block, receipt_block
    assert json.dumps(receipt_block, sort_keys=True) == json.dumps(producer_block, sort_keys=True)
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
        calibration_overrides_path=_NO_DECLARATION,
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
        calibration_overrides_path=_NO_DECLARATION,
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


def _write_healthy_basin_pair(basins_root: Path) -> None:
    """Real two-basin tree, both publishable.

    Both basins carry the same ``*.tsd.lai`` header, which is what the radiation
    template matcher keys on, so alpha's ``*.tsd.rl`` is a valid template for
    bravo once bravo loses its own.
    """
    from tests.test_basins_registry_import import _make_valid_model

    lai_header = "900\t18\t19810101\t20551201\t86400\n"
    for slug in ("alpha", "bravo"):
        input_dir = _make_valid_model(basins_root / slug, slug, sp_segment_count=2)
        (input_dir / f"{slug}.tsd.lai").write_text(f"{lai_header}lai\n", encoding="utf-8")
        (input_dir / f"{slug}.tsd.rl").write_text(f"{lai_header}radiation\n", encoding="utf-8")


def _break_bravo_beyond_repair(basins_root: Path) -> None:
    """Make ``bravo`` unpublishable in a way the radiation repair cannot fix.

    It loses ``*.tsd.rl`` (so the repair picks it up, using alpha's file as the
    template) AND gains #1197's malformed ``23106\\t6`` IC header, which discovery
    refuses on the repaired copy too.  Nothing about that refusal is bravo-specific:
    it is the shape of "one basin in the tree is unpublishable for a reason the
    repair does not address".
    """
    bravo_input = basins_root / "bravo" / "input" / "bravo"
    (bravo_input / "bravo.tsd.rl").unlink()
    (bravo_input / "bravo.cfg.ic").write_text("23106\t6\n1\t0.1\n", encoding="utf-8")


def _write_radiation_repair_pair(basins_root: Path) -> None:
    """Healthy ``alpha`` + ``bravo`` that no repair can save."""
    _write_healthy_basin_pair(basins_root)
    _break_bravo_beyond_repair(basins_root)


def test_bulk_publish_skips_a_repaired_model_that_is_still_unpublishable(tmp_path: Path) -> None:
    """B1: one unsalvageable basin must not take the whole bulk publish down.

    The radiation repair is a best-effort rescue of models plain selection already
    dropped.  Raising when the rescue fails is right for an EXPLICIT request and
    wrong for a bulk run: production's registry refresh and the node-27 runbook
    both publish unfiltered, so a single malformed IC in the tree would leave the
    scheduler registry with zero models — a strictly worse terminal state than the
    one basin that is actually broken.
    """
    basins_root = tmp_path / "Basins"
    _write_radiation_repair_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    work_dir = tmp_path / "work"

    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
    )

    assert summary["status"] == "published"
    assert summary["selected_basin_slugs"] == ["alpha"]
    assert summary["repairs"] == []
    payload = json.loads(registry_manifest.read_text(encoding="utf-8"))
    assert {row["model_id"] for row in payload["models"]} == {"basins_alpha_shud"}
    # Skipped, not silent: the run's own inventory keeps bravo's refusal reason.
    inventory = json.loads((work_dir / "basins-inventory.json").read_text(encoding="utf-8"))
    bravo = next(model for model in inventory["models"] if model["basin_slug"] == "bravo")
    assert bravo["status"] == "partial"
    assert bravo["missing_required_files"] == ["*.tsd.rl"]
    assert any("2 numeric token(s)" in reason for reason in bravo["invalid_required_files"])


def test_explicitly_requested_unsalvageable_model_still_fails_closed(tmp_path: Path) -> None:
    """The other half of B1: an operator who NAMES the basin gets the refusal."""
    basins_root = tmp_path / "Basins"
    _write_radiation_repair_pair(basins_root)

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json",
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-filtered",
            basin_slugs=["bravo"],
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_REPAIRED_MODEL_NOT_PUBLISHABLE"
    details = excinfo.value.details
    assert details["basin_slug"] == "bravo"
    assert any("2 numeric token(s)" in reason for reason in details["invalid_required_files"])


def test_bulk_skip_of_an_already_registered_model_is_refused_by_the_cutover_gate(
    tmp_path: Path,
) -> None:
    """The bulk skip is NOT a licence to drop a model that is already published.

    The two tests above cover the bootstrap shape (nothing registered yet), where
    skipping bravo is the whole point.  Production's refresh lane runs the same
    bulk publish behind #1080's registry-cutover gate, and there the skip means
    something else: the canonical registry HAS ``basins_bravo_shud``, the
    prospective one does not, so the gate classifies it as a removal and refuses
    the whole canonical replacement (``registry_cutover_removal_refused``).  The
    refresh lane has no bypass for that — only the manual CLI's
    ``--allow-uncovered-cutover``.  So the skip's real terminal state, once a
    basin has ever been published, is a failed run with the previous registry
    intact, and this test pins that rather than the comment's word for it.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    first = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    assert first["status"] == "published"
    previous_bytes = registry_manifest.read_bytes()
    assert {row["model_id"] for row in json.loads(previous_bytes)["models"]} == {
        "basins_alpha_shud",
        "basins_bravo_shud",
    }

    _break_bravo_beyond_repair(basins_root)

    # Wire the real #1080 gate exactly as scheduler_file_provider_refresh does:
    # the previous canonical bytes/digest snapshot taken before the run, no
    # cutover declaration, real publish.
    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=None,
            dry_run=False,
            classification_sink=classification.update,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-refresh",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED"
    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"
    assert excinfo.value.details["provider_phase"] == "precommit"
    assert classification["removed"]["items"] == ["basins_bravo_shud"]
    assert [entry["model_id"] for entry in classification["refused"]["items"]] == ["basins_bravo_shud"]
    assert {entry["reason"] for entry in classification["refused"]["items"]} == {
        "registry_cutover_removal_refused"
    }
    # Canonical registry survives untouched: alpha is not republished alone.
    assert registry_manifest.read_bytes() == previous_bytes


def test_undeclared_removal_refusal_carries_the_skip_cause_evidence(
    tmp_path: Path,
) -> None:
    """#1433 branch (a): the refusal above stays fail-closed AND says why.

    Same bulk skip, same refusal, previous canonical bytes intact, nothing
    published — the only addition is that the refusal entry now carries the
    inventory row the publisher skipped on (``status`` /
    ``missing_required_files`` / ``invalid_required_files``), so an operator can
    tell an invalid package from a deleted model directory without digging
    through a workspace the refresh lane deletes.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    previous_bytes = registry_manifest.read_bytes()

    _break_bravo_beyond_repair(basins_root)

    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}
    skipped_models: dict[str, Mapping[str, Any]] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=None,
            dry_run=False,
            classification_sink=classification.update,
            now=generated_at,
            skipped_models=skipped_models,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-refresh",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
            skipped_model_sink=skipped_models.update,
        )

    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"
    assert registry_manifest.read_bytes() == previous_bytes
    assert classification["declared_retirements"]["total"] == 0
    refusal = classification["refused"]["items"][0]
    assert refusal["model_id"] == "basins_bravo_shud"
    assert refusal["reason"] == "registry_cutover_removal_refused"
    assert refusal["status"] == "partial"
    # The row is the POST-repair one: the radiation repair restored bravo's
    # ``*.tsd.rl`` from alpha's template, so what is left is what actually
    # blocks the publish — the malformed IC header.
    assert refusal["missing_required_files"] == []
    assert any("2 numeric token(s)" in reason for reason in refusal["invalid_required_files"])


def test_declared_retirement_lets_the_refresh_publish_without_the_skipped_model(
    tmp_path: Path,
) -> None:
    """#1433: the retire declaration is the way out of the removal deadlock.

    Same tree, same gate wiring, and the same bulk skip as the refusal test
    above.  The only difference is a declaration naming ``basins_bravo_shud``
    with ``transition_mode: "retire"`` and ``new_checksum: null``: the gate then
    admits the removal, the refresh publishes, canonical loses exactly bravo's
    row, and alpha publishes normally.

    The first (refused) run is the runbook's step 2, executed the way an
    operator executes it: a real refresh that fails closed, whose receipt
    carries BOTH values the declaration needs — the generation
    (``registry_classification.generation``) and the previous canonical
    ``old_checksum`` (on the removal refusal row).  The test reads them from the
    classification exactly as the runbook says to, so a regression that stops
    publishing the generation reddens here instead of stranding an operator.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    previous_bytes = registry_manifest.read_bytes()
    previous_rows = {row["model_id"]: row for row in json.loads(previous_bytes)["models"]}
    assert set(previous_rows) == {"basins_alpha_shud", "basins_bravo_shud"}

    _break_bravo_beyond_repair(basins_root)

    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}
    declaration_env: dict[str, str] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=declaration_env.get("path"),
            dry_run=False,
            classification_sink=classification.update,
            now=generated_at,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-undeclared",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
        )
    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"

    # Step 2 of the runbook: both declaration inputs come off THIS receipt.
    refused_generation = classification["generation"]
    assert refused_generation is not None
    refused_removal = next(
        item
        for item in classification["refused"]["items"]
        if item["reason"] == "registry_cutover_removal_refused"
    )
    assert refused_removal["old_checksum"] == previous_rows["basins_bravo_shud"]["package_checksum"]

    declaration = tmp_path / "retire-declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-08-16T00:00:00Z",
                "generation": refused_generation,
                "entries": [
                    {
                        "model_id": "basins_bravo_shud",
                        "old_checksum": refused_removal["old_checksum"],
                        "new_checksum": None,
                        "effective_cycle_utc": "2026-08-16T12:00:00Z",
                        "transition_mode": "retire",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    declaration_env["path"] = str(declaration)

    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-retire",
        registry_generated_at=generated_at,
        precommit_validator=precommit_provider_generation,
    )

    assert summary["status"] == "published"
    published_rows = json.loads(registry_manifest.read_text(encoding="utf-8"))["models"]
    assert {row["model_id"] for row in published_rows} == {"basins_alpha_shud"}
    assert classification["removed"]["items"] == ["basins_bravo_shud"]
    assert classification["refused"]["total"] == 0
    retired = classification["declared_retirements"]["items"]
    assert [entry["model_id"] for entry in retired] == ["basins_bravo_shud"]
    assert retired[0]["old_checksum"] == previous_rows["basins_bravo_shud"]["package_checksum"]
    assert retired[0]["new_checksum"] is None
    assert retired[0]["transition_mode"] == "retire"
    # The generation the operator copied off the refusal is the one the
    # accepting run bound to — that identity is what makes the runbook loop
    # terminate instead of chasing a moving value.
    assert classification["generation"] == refused_generation


def test_repaired_package_is_reused_across_run_scoped_workspaces(tmp_path: Path) -> None:
    from tests.test_basins_registry_import import _write_registry_fixture

    basins_root, input_dir, _inventory_path, _manifest_path, model_id = _write_registry_fixture(
        tmp_path / "fixture"
    )
    repair_template = _write_soil_alpha_model_files(
        tmp_path / "repair-template",
        "basin-a",
        "alias-a",
    )
    for suffix in ("cfg.calib", "para.soil"):
        (input_dir / f"alias-a.{suffix}").write_bytes(
            (repair_template / f"alias-a.{suffix}").read_bytes()
        )

    object_root = tmp_path / "object-store"
    first_registry = tmp_path / "providers" / "first.json"
    second_registry = tmp_path / "providers" / "second.json"
    first = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=first_registry,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "run-one" / "registry",
        repair_missing_radiation=False,
    )
    second = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=second_registry,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "run-two" / "registry",
        repair_missing_radiation=False,
    )

    assert first["package_status_counts"] == {"published": 1}
    assert second["package_status_counts"] == {"already_done": 1}
    assert first["packages"][0]["version"] == second["packages"][0]["version"]
    row = json.loads(second_registry.read_text(encoding="utf-8"))["models"][0]
    assert row["model_id"] == model_id
    assert row["resource_profile"]["source_path"] == str(basins_root / "basin-a")
    assert "run-one" not in json.dumps(row)
    assert "run-two" not in json.dumps(row)
    # #1816 spec scenario "publication is a pure copy with respect to
    # calibration": two runs from an unchanged source, both byte-identical to
    # the source `cfg.calib` -- even though it is outside the deleted bounds.
    source_calibration = (input_dir / "alias-a.cfg.calib").read_bytes()
    for run_name, work_dir in (("run-one", tmp_path / "run-one" / "registry"),
                               ("run-two", tmp_path / "run-two" / "registry")):
        assert _published_calibration_bytes(
            work_dir=work_dir, object_root=object_root, model_id=model_id
        ) == source_calibration, run_name


def _write_out_of_bounds_calibration_fixture(tmp_path: Path, *, parameter: str) -> tuple[Path, Path, str]:
    """A real publishable basin whose ``cfg.calib`` sits outside the deleted bounds.

    ``SHUD_SOIL_ALPHA_MAX = 20.0`` / ``SHUD_GEOL_DMAC_MAX = 4.0`` had no source
    anywhere in the repository (#1816); the calibrations they overrode were
    produced by external users running SHUD to convergence.  Publication must
    now copy them through untouched.
    """

    from tests.test_basins_registry_import import _write_registry_fixture

    basins_root, input_dir, _inventory_path, _manifest_path, model_id = _write_registry_fixture(
        tmp_path / "fixture"
    )
    if parameter == "SOIL_ALPHA":
        template = _write_soil_alpha_model_files(tmp_path / "calibration-template", "basin-a", "alias-a")
        para_suffix = "para.soil"
    else:
        template = _write_geol_dmac_model_files(tmp_path / "calibration-template", "basin-a", "alias-a")
        para_suffix = "para.geol"
    for suffix in ("cfg.calib", para_suffix):
        (input_dir / f"alias-a.{suffix}").write_bytes((template / f"alias-a.{suffix}").read_bytes())
    return basins_root, input_dir / "alias-a.cfg.calib", model_id


def _published_calibration_bytes(*, work_dir: Path, object_root: Path, model_id: str) -> bytes:
    manifest = json.loads(
        (work_dir / "package-manifests" / f"{model_id}.manifest.json").read_text(encoding="utf-8")
    )
    calibration_files = [
        item for item in manifest["included_files"] if str(item["relative_path"]).endswith(".cfg.calib")
    ]
    assert len(calibration_files) == 1, calibration_files
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    return store.read_bytes(str(calibration_files[0]["object_uri"]))


def _publish_one_basin(*, basins_root: Path, tmp_path: Path, run_name: str) -> tuple[dict[str, Any], Path, Path]:
    object_root = tmp_path / "object-store"
    work_dir = tmp_path / run_name / "registry"
    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=tmp_path / "providers" / f"{run_name}.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
        repair_missing_radiation=False,
        retain_repair_staging=True,
    )
    return summary, work_dir, object_root


def test_published_calibration_is_byte_identical_for_out_of_bounds_soil_alpha(tmp_path: Path) -> None:
    """#1816 §1.1: an over-bound ``SOIL_ALPHA`` publishes unchanged."""
    basins_root, source_calib, model_id = _write_out_of_bounds_calibration_fixture(
        tmp_path, parameter="SOIL_ALPHA"
    )
    source_bytes = source_calib.read_bytes()
    assert b"SOIL_ALPHA\t8.19327372615961" in source_bytes

    summary, work_dir, object_root = _publish_one_basin(
        basins_root=basins_root, tmp_path=tmp_path, run_name="run-soil-alpha"
    )

    # Shared repair plumbing is untouched by the deletion: the staging-retention
    # switch still reports, it simply has no calibration staging to retain.
    assert summary["repair_staging_cleanup"] == {"status": "retained", "reason": "retain_repair_staging"}
    assert source_calib.read_bytes() == source_bytes
    assert (
        _published_calibration_bytes(work_dir=work_dir, object_root=object_root, model_id=model_id)
        == source_bytes
    )


def test_published_calibration_is_byte_identical_for_out_of_bounds_geol_dmac(tmp_path: Path) -> None:
    """#1816 §1.2: same for ``GEOL_DMAC``."""
    basins_root, source_calib, model_id = _write_out_of_bounds_calibration_fixture(
        tmp_path, parameter="GEOL_DMAC"
    )
    source_bytes = source_calib.read_bytes()
    assert b"GEOL_DMAC\t5" in source_bytes

    _summary, work_dir, object_root = _publish_one_basin(
        basins_root=basins_root, tmp_path=tmp_path, run_name="run-geol-dmac"
    )

    assert source_calib.read_bytes() == source_bytes
    assert (
        _published_calibration_bytes(work_dir=work_dir, object_root=object_root, model_id=model_id)
        == source_bytes
    )


def test_publish_records_no_calibration_repair(tmp_path: Path) -> None:
    """#1816 §1.3: no publication artefact claims a calibration repair."""
    basins_root, _source_calib, model_id = _write_out_of_bounds_calibration_fixture(
        tmp_path, parameter="SOIL_ALPHA"
    )

    summary, work_dir, _object_root = _publish_one_basin(
        basins_root=basins_root, tmp_path=tmp_path, run_name="run-no-repair"
    )

    assert summary["repairs"] == []
    package_manifest = (work_dir / "package-manifests" / f"{model_id}.manifest.json").read_text(
        encoding="utf-8"
    )
    assert "calibration_repair" not in package_manifest


def _published_bytes_for_suffix(
    *, work_dir: Path, object_root: Path, model_id: str, suffix: str
) -> bytes:
    manifest = json.loads(
        (work_dir / "package-manifests" / f"{model_id}.manifest.json").read_text(encoding="utf-8")
    )
    matches = [
        item for item in manifest["included_files"] if str(item["relative_path"]).endswith(suffix)
    ]
    assert len(matches) == 1, matches
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    return store.read_bytes(str(matches[0]["object_uri"]))


def test_radiation_repair_supplies_template_without_touching_calibration(tmp_path: Path) -> None:
    """#1816 s1.4: the two halves of the radiation-repair scenario hold together.

    Scenario "A missing radiation template is still supplied and recorded"
    asserts a conjunction: the template IS added AND the calibration is NOT
    touched.  Both halves must be observed on the same real (non-mocked)
    publish, on a basin whose ``cfg.calib`` sits outside the deleted bound --
    otherwise "we add files but never rewrite values" is only ever tested one
    clause at a time.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    bravo_input = basins_root / "bravo" / "input" / "bravo"
    # bravo is missing ONLY *.tsd.rl -- exactly the repairable shape.
    (bravo_input / "bravo.tsd.rl").unlink()
    template = _write_soil_alpha_model_files(tmp_path / "calibration-template", "basin-a", "alias-a")
    for suffix in ("cfg.calib", "para.soil"):
        (bravo_input / f"bravo.{suffix}").write_bytes((template / f"alias-a.{suffix}").read_bytes())

    source_calib = bravo_input / "bravo.cfg.calib"
    source_bytes = source_calib.read_bytes()
    assert b"SOIL_ALPHA\t8.19327372615961" in source_bytes
    radiation_template_bytes = (basins_root / "alpha" / "input" / "alpha" / "alpha.tsd.rl").read_bytes()

    object_root = tmp_path / "objects"
    work_dir = tmp_path / "work"
    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
        repair_missing_radiation=True,
    )

    # Guard against a vacuous pass: bravo must actually have been repaired and
    # published, not skipped.
    assert summary["selected_basin_slugs"] == ["alpha", "bravo"]
    assert summary["package_status_counts"] == {"published": 2}
    model_id = "basins_bravo_shud"

    # Bullet 1: the package carries the supplied template, byte-for-byte.
    assert (
        _published_bytes_for_suffix(
            work_dir=work_dir, object_root=object_root, model_id=model_id, suffix=".tsd.rl"
        )
        == radiation_template_bytes
    )
    # Bullet 2: the run records the repair under the radiation schema.
    assert len(summary["repairs"]) == 1
    repair = summary["repairs"][0]
    assert repair["schema_version"] == "basins.missing_tsd_rl_template_repair.v1"
    assert repair["basin_slug"] == "bravo"
    assert [item["status"] for item in repair["repairs"]] == ["repaired"]
    # Bullet 3: the calibration rode through untouched, at source and published.
    assert source_calib.read_bytes() == source_bytes
    assert (
        _published_calibration_bytes(work_dir=work_dir, object_root=object_root, model_id=model_id)
        == source_bytes
    )


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


# ---------------------------------------------------------------------------
# Round-2 fix pass (#1080): manual publisher CLI now wires the cutover gate.
#
# The former round-1 scaffolding "gate refusal preserves canonical bytes under
# the same lock" test was removed per R2-N6 in the round-2 review: it wrapped
# the publisher in a test-owned destination lock but did NOT prove the
# publisher itself acquires that same canonical lock at replace time, so the
# concurrency invariant it claimed to test lived entirely in test scaffolding.
# The truthful coverage lives in
# `tests/test_scheduler_file_provider_refresh.py::test_full_runner_refresh_lock_is_held_during_precommit_gate`
# (T13, part a) which instruments the runner's real `refresh_lock` and proves
# a competing non-blocking acquire fails while the gate runs.
# ---------------------------------------------------------------------------


def test_manual_cli_refuses_undeclared_package_cutover_without_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T12 / C-D1: the manual CLI now refuses when the prospective set has
    a ``package_changed`` row and no cutover declaration is filed.  Without
    ``--allow-uncovered-cutover`` the wired gate must reject; the previous
    canonical bytes remain intact."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-13T00:00:00Z",
                # Same model_id as the prospective row, but a different
                # package_checksum -> package_changed.
                "models": [
                    {
                        "model_id": "basins_first_shud",
                        "basin_id": "basins_first",
                        "model_package_uri": "s3://nhms/models/basins_first_shud/OLD/package/",
                        "manifest_uri": "s3://nhms/models/basins_first_shud/OLD/manifest.json",
                        "package_checksum": "package-sha-basins_first_shud-OLD",
                    }
                ],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    before = canonical.read_bytes()
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
    ]

    exit_code = registry_script.main(argv)
    assert exit_code != 0
    captured = capsys.readouterr()
    err = captured.err
    # The refusal payload includes the wired-gate provider_reason.
    assert "registry_cutover_undeclared" in err, err
    # Canonical bytes untouched.
    assert canonical.read_bytes() == before
    # R2-A1: the refusal error payload records the cutover_gate audit fact
    # (enforced, declaration_present=False) so a later auditor reading stderr
    # can distinguish "gate ran and refused" from "gate was skipped".
    refusal_payload = json.loads(err.strip().splitlines()[-1])
    audit = refusal_payload.get("cutover_gate")
    assert audit == {
        "mode": "enforced",
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": False,
    }, refusal_payload


def test_manual_cli_allow_uncovered_bypasses_gate_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T12 / C-D1: ``--allow-uncovered-cutover`` bypasses the gate and
    prints a stderr WARNING banner.  This is the bootstrap/one-off recovery
    seam, not a regular publish path."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(canonical.parent)
    write_provider_destination(
        canonical,
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-13T00:00:00Z",
                "models": [
                    {
                        "model_id": "basins_first_shud",
                        "basin_id": "basins_first",
                        "model_package_uri": "s3://nhms/models/basins_first_shud/OLD/package/",
                        "manifest_uri": "s3://nhms/models/basins_first_shud/OLD/manifest.json",
                        "package_checksum": "package-sha-basins_first_shud-OLD",
                    }
                ],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n",
    )
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
        "--allow-uncovered-cutover",
    ]
    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    err = captured.err
    out = captured.out
    # #1104 (P2-4): a bare `"WARNING" in err` became vacuous once every run
    # emits the operator-gate startup warning, so pin the bypass banner itself.
    assert "WARNING: --allow-uncovered-cutover disables the #1080 registry" in err
    assert "allow-uncovered-cutover" in err
    # With bypass, publish should proceed (canonical bytes replaced).
    assert exit_code == 0
    assert canonical.read_bytes() != json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-13T00:00:00Z",
        },
        sort_keys=True,
    ).encode()
    # R2-A1: the summary emitted to stdout records the bypass on
    # `cutover_gate.mode` alongside the stderr WARNING, so persisted CLI
    # output distinguishes a bypass run from a gate-passing run without
    # relying on the ephemeral WARNING line.
    summary = json.loads(out)
    assert (
        summary["schema_version"]
        == "nhms.scheduler.basins_file_registry_publish.v2"
    )
    assert summary["cutover_gate"] == {
        "mode": "bypassed_allow_uncovered_cutover",
        "declaration_env": None,
        "declaration_present": False,
    }
    # The bypass audit fact also surfaces on the manifest publication
    # receipt so downstream operators reading `manifest-last.json`'s
    # companion receipt see the same fact.
    assert summary["registry"]["cutover_gate"] == summary["cutover_gate"]


def test_manual_cli_records_declaration_present_when_env_resolves_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R2-A1: when the CLI runs with the gate enforced AND the operator has
    staged a declaration file the runner can open, the persisted CLI output
    (summary on happy path, error payload on refusal path) records
    ``cutover_gate={mode:enforced, declaration_env:<env>,
    declaration_present:true}``.  This closes the byte-identical hole where
    an enforced-with-declaration run and an enforced-no-declaration run
    would otherwise be indistinguishable in later audit.

    Using bootstrap-shaped setup (no previous canonical) with a schema-valid
    but generation-mismatched declaration: the gate correctly refuses on
    declaration_invalid, and the assertion under test is that the audit
    still reports ``declaration_present=True`` — the audit is about "did
    the operator stage a file the runner could open", not "did the gate
    ultimately accept it"."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    # Schema-valid declaration file (readable, correct JSON shape) but
    # deliberately-unmatched generation — the audit still records
    # `declaration_present=True` because the operator DID stage a file the
    # runner could open.  A separate audit fact would record the gate's
    # decision on the declaration's semantic validity.
    declaration_path = tmp_path / "declarations" / "cutover.json"
    declaration_path.parent.mkdir(parents=True)
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-07-14T12:00:00Z",
                "generation": "manifest-000000000000",
                "entries": [],
            },
            sort_keys=True,
        )
        + "\n"
    )
    monkeypatch.setenv(
        "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", str(declaration_path)
    )

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
    ]
    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    # Either exit code is acceptable here; the audit fact is what's under
    # test.  Read from whichever channel carries the payload.
    payload_json = captured.out if exit_code == 0 else captured.err.strip().splitlines()[-1]
    payload = json.loads(payload_json)
    assert payload["cutover_gate"] == {
        "mode": "enforced",
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": True,
    }, payload


# ---------------------------------------------------------------------------
# #1097: cutover_gate audit on the manifest channel
# ---------------------------------------------------------------------------


_MALFORMED_CUTOVER_GATES: list[Any] = [
    pytest.param(["enforced"], id="non_mapping"),
    pytest.param({"mode": ""}, id="empty_mode"),
    pytest.param({"mode": "gate_off"}, id="mode_outside_audited_set"),
    pytest.param(
        {"mode": "enforced", "declaration_env": 42, "declaration_present": True},
        id="non_string_declaration_env",
    ),
    pytest.param(
        {"mode": "enforced", "declaration_env": "E", "declaration_present": "no"},
        id="non_bool_declaration_present",
    ),
]


def _registry_destination(tmp_path: Path) -> Path:
    destination = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(destination.parent)
    return destination


@pytest.mark.parametrize("cutover_gate", _MALFORMED_CUTOVER_GATES)
def test_manifest_publish_refuses_malformed_cutover_gate_before_commit(
    tmp_path: Path,
    cutover_gate: Any,
) -> None:
    """#1097: the manifest channel is fail-closed on a malformed audit block.

    Before the unification it mirrored the block leniently AFTER the commit —
    an empty/unknown mode was silently rewritten to ``"not_wired"`` and the
    manifest was published anyway, so operators read contradictory audit facts
    from the companion receipt and the CLI summary.  Now the shared strict
    normalizer runs before the manifest bytes are committed.
    """
    destination = _registry_destination(tmp_path)

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        publish_scheduler_registry_manifest(
            [],
            destination,
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            cutover_gate=cutover_gate,
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert not destination.exists(), "malformed audit input must not commit a manifest"


def test_manifest_publish_leaves_previous_bytes_intact_on_malformed_cutover_gate(
    tmp_path: Path,
) -> None:
    """#1097: with a manifest already in place the refusal is a no-op — the
    previously canonical bytes stay byte-identical (spec scenario 1's
    "absent or unchanged" other half)."""
    destination = _registry_destination(tmp_path)
    previous = publish_scheduler_registry_manifest(
        [],
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        cutover_gate={
            "mode": "enforced",
            "declaration_env": registry_script.CUTOVER_DECLARATION_ENV_NAME,
            "declaration_present": True,
        },
    )
    before = destination.read_bytes()
    assert previous["cutover_gate"]["mode"] == "enforced"

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        publish_scheduler_registry_manifest(
            [],
            destination,
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            cutover_gate={"mode": "gate_off"},
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert destination.read_bytes() == before


def test_direct_manifest_publish_without_cutover_gate_omits_the_receipt_key(
    tmp_path: Path,
) -> None:
    """#1097 / spec scenario 4: the direct callers that never wire the gate
    (worker mirror, require-direct-grid, direct-grid provisioning) keep the
    pre-existing key-omitting receipt shape — ``None`` must NOT be embedded as
    a ``not_wired`` block on this entry point."""
    destination = _registry_destination(tmp_path)

    receipt = publish_scheduler_registry_manifest(
        [],
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        cutover_gate=None,
    )

    assert "cutover_gate" not in receipt, receipt
    assert destination.is_file()


def test_aggregate_publish_without_cutover_gate_records_not_wired_on_both_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1097 / spec scenario 3: the CLI aggregate entry normalizes at its own
    boundary, so an unwired run records the same ``not_wired`` block on the
    summary AND on the manifest companion receipt (unlike the direct manifest
    caller above, which omits the key)."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
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
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=tmp_path / "Basins",
        registry_manifest=tmp_path / "objects/scheduler/registry/manifest-last.json",
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
        cutover_gate=None,
    )

    not_wired = {
        "mode": "not_wired",
        "declaration_env": None,
        "declaration_present": False,
    }
    assert summary["cutover_gate"] == not_wired
    assert summary["registry"]["cutover_gate"] == not_wired




# ---------------------------------------------------------------------------
# #1132: CLI failure diagnostics route through the shared normalizer
# ---------------------------------------------------------------------------


_CLI_FAILURE_TRIGGERS = (
    "registry_publish_error",
    "basins_discovery_error",
    "file_provider_error",
)
# A legal three-field block: an illegal sentinel would be refused by the
# un-stubbed services-side normalizer and the CLI would fail down a different
# branch (pass for the wrong reason).
_NORMALIZER_SENTINEL = {
    "mode": "not_wired",
    "declaration_env": "SENTINEL_ENV",
    "declaration_present": False,
}


def _install_cli_failure(
    trigger: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive ``main`` into one specific stderr failure branch, cutover-free.

    Each trigger is a deterministic error with no cutover-gate involvement, so
    the payload assertion cannot pass because of an unrelated refusal.
    """
    options = {
        "--basins-root": str(tmp_path / "Basins"),
        "--registry-manifest": str(tmp_path / "shared/scheduler/registry/manifest-last.json"),
        "--object-store-root": str(tmp_path / "private-objects"),
        "--object-store-prefix": "s3://nhms",
        "--work-dir": str(tmp_path / "work"),
    }
    if trigger == "registry_publish_error":
        monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
        options["--object-store-prefix"] = ""
    elif trigger == "basins_discovery_error":
        options["--basins-root"] = str(tmp_path / "missing-basins")
    else:
        def raise_provider_error(**kwargs: object) -> dict[str, Any]:
            del kwargs
            raise registry_script.SchedulerFileProviderError(
                "registry_manifest_invalid", field="models", evidence={"phase": "publish"}
            )

        monkeypatch.setattr(
            registry_script, "publish_all_basin_scheduler_registry", raise_provider_error
        )
    return [item for pair in options.items() for item in pair]


@pytest.mark.parametrize("trigger", _CLI_FAILURE_TRIGGERS)
def test_cli_failure_payload_cutover_gate_is_produced_by_shared_normalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trigger: str,
) -> None:
    """#1132: every stderr failure payload must carry the SHARED normalizer's
    output, not an inline literal.

    Pins the call, not the value: the legal literals the CLI builds are fixed
    points of normalization, so a value-equality assertion stays green against
    an implementation that never calls the normalizer.  Stubbing the normalizer
    to return a sentinel is the only assertion that bites.
    """
    monkeypatch.setattr(
        registry_script,
        "normalize_cutover_gate_audit",
        lambda cutover_gate: dict(_NORMALIZER_SENTINEL),
    )
    argv = _install_cli_failure(trigger, tmp_path, monkeypatch)

    assert registry_script.main(argv) == 1

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["cutover_gate"] == _NORMALIZER_SENTINEL, payload


# ---------------------------------------------------------------------------
# #1104: manual-publisher concurrency is operator-gated, not CAS-gated.
#
# `main()` never populates `expected_preimage`, so nothing in code stops the
# manual CLI from overwriting a refresh commit that lands between its snapshot
# and its own commit.  The mitigation is an explicit runbook prohibition, and
# the CLI must point every operator at it on startup.  These pins prove the
# warning reaches stderr on both a successful and a failing run, and that the
# machine-readable failure payload still parses from the final stderr line.
# ---------------------------------------------------------------------------

_REFRESH_TIMER_UNIT = "nhms-scheduler-file-provider-refresh.timer"

# Captured from the pre-#1104 CLI for the `registry_publish_error` trigger:
# the startup warning must not perturb one byte of this payload.
_PREFIX_MISSING_FAILURE_PAYLOAD = {
    "cutover_gate": {
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": False,
        "mode": "enforced",
    },
    "error_code": "SCHEDULER_REGISTRY_OBJECT_STORE_PREFIX_MISSING",
    "message": "OBJECT_STORE_PREFIX or --object-store-prefix is required.",
}


def test_cli_prints_operator_gate_warning_on_successful_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1104 / 2.1: a run that publishes successfully still emits the
    operator-gate startup warning as its FIRST stderr line, naming the refresh
    timer unit, and leaves the stdout summary untouched."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(canonical.parent)
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
        # Bootstrap bypass: the only deterministic exit-0 CLI path in this
        # suite.  The pin below is on the startup warning, which is emitted
        # before the bypass branch is even evaluated.
        "--allow-uncovered-cutover",
    ]

    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    startup_line = captured.err.strip().splitlines()[0]

    assert _REFRESH_TIMER_UNIT in startup_line, captured.err
    assert "WARNING" in startup_line, captured.err
    # P2-4: the startup warning must stay distinguishable from the
    # `--allow-uncovered-cutover` bypass banner, which is asserted separately.
    assert "allow-uncovered-cutover" not in startup_line, captured.err
    assert exit_code == 0
    # The warning is stderr-only: the stdout summary contract is unchanged.
    summary = json.loads(captured.out)
    assert summary["schema_version"] == "nhms.scheduler.basins_file_registry_publish.v2"


def test_cli_startup_warning_coexists_with_failure_json_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1104 / 2.2: on a deterministic failure the startup warning leads
    stderr and the existing JSON error payload still parses byte-for-byte
    identically from the final stderr line — the whole suite (and node-22
    operators) read that channel with ``strip().splitlines()[-1]``."""
    argv = _install_cli_failure("registry_publish_error", tmp_path, monkeypatch)

    assert registry_script.main(argv) == 1

    err = capsys.readouterr().err
    lines = err.strip().splitlines()
    assert _REFRESH_TIMER_UNIT in lines[0], err
    assert json.loads(lines[-1]) == _PREFIX_MISSING_FAILURE_PAYLOAD, err
    # Exactly one added line: warning + payload, nothing else on the channel.
    assert len(lines) == 2, err


# ---------------------------------------------------------------------------
# #1832: declared calibration overrides.
#
# #1816 deleted a publisher step that scanned every basin and silently clamped
# calibration values against two hard-coded bounds.  Deleting it was right in
# substance, but one of the two bounds (`GEOL_DMAC <= 4`) is a real empirical
# stability bound: with its source value 5, `hetianhe` makes SHUD produce NaN
# and exit 10.  These tests pin the replacement -- an explicit, declared,
# recorded exception -- and, above all, the properties #1816 existed to
# protect: nothing undeclared is touched, and the Basins source tree is only
# ever read.
# ---------------------------------------------------------------------------

_SOURCE_CALIB_TEXT = "GEOL_KSATH\t0.00977999747288218\nGEOL_DMAC\t5\nSOIL_ALPHA\t8.19327372615961\n"
_OVERRIDE_REASON = "GEOL_DMAC 5 and 4.75 both NaN/EXIT 10; 4.5 and 4 run clean on gfs and IFS."


def _write_declaration(path: Path, entries: list[dict[str, Any]]) -> Path:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"calibration_overrides": entries}, sort_keys=False), encoding="utf-8")
    return path


def _declaration_entry(
    *,
    basin_slug: str = "alpha",
    parameter: str = "GEOL_DMAC",
    value: Any = 4,
) -> dict[str, Any]:
    return {
        "basin_slug": basin_slug,
        "parameter": parameter,
        "value": value,
        "reason": _OVERRIDE_REASON,
        "approver": "danker",
        "date": "2026-08-24",
    }


def _write_override_fixture(tmp_path: Path) -> Path:
    """Two publishable basins; only ``alpha`` is ever declared."""
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    for slug in ("alpha", "bravo"):
        (basins_root / slug / "input" / slug / f"{slug}.cfg.calib").write_text(
            _SOURCE_CALIB_TEXT, encoding="utf-8"
        )
    return basins_root


def _write_basin_with_source_calibration(basins_root: Path, slug: str) -> None:
    """One more publishable basin carrying ``_SOURCE_CALIB_TEXT``."""
    from tests.test_basins_registry_import import _make_valid_model

    lai_header = "900\t18\t19810101\t20551201\t86400\n"
    input_dir = _make_valid_model(basins_root / slug, slug, sp_segment_count=2)
    (input_dir / f"{slug}.tsd.lai").write_text(f"{lai_header}lai\n", encoding="utf-8")
    (input_dir / f"{slug}.tsd.rl").write_text(f"{lai_header}radiation\n", encoding="utf-8")
    (input_dir / f"{slug}.cfg.calib").write_text(_SOURCE_CALIB_TEXT, encoding="utf-8")


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _publish_with_declaration(
    *,
    basins_root: Path,
    tmp_path: Path,
    declaration: Path | None,
    run_name: str,
) -> tuple[dict[str, Any], Path, Path]:
    object_root = tmp_path / run_name / "objects"
    work_dir = tmp_path / run_name / "work"
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=tmp_path / run_name / "providers" / "manifest-last.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
        calibration_overrides_path=declaration,
    )
    return summary, work_dir, object_root


def _package_manifest(work_dir: Path, model_id: str) -> dict[str, Any]:
    return json.loads(
        (work_dir / "package-manifests" / f"{model_id}.manifest.json").read_text(encoding="utf-8")
    )


def test_undeclared_basin_publishes_its_calibration_unchanged(tmp_path: Path) -> None:
    """#1832 spec scenario 1: absent from the declaration -> pure byte copy.

    ``bravo`` carries the same out-of-bound ``GEOL_DMAC 5`` as ``alpha``; the
    only difference between them is that the declaration names one of them.
    That is the whole point of "named, not scanned".
    """
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(tmp_path / "config" / "overrides.yaml", [_declaration_entry()])
    source_bytes = (basins_root / "bravo" / "input" / "bravo" / "bravo.cfg.calib").read_bytes()

    summary, work_dir, object_root = _publish_with_declaration(
        basins_root=basins_root, tmp_path=tmp_path, declaration=declaration, run_name="undeclared"
    )

    assert summary["selected_basin_slugs"] == ["alpha", "bravo"]
    published = _published_calibration_bytes(
        work_dir=work_dir, object_root=object_root, model_id="basins_bravo_shud"
    )
    assert published == source_bytes
    assert b"GEOL_DMAC\t5" in published
    # Absence, not an empty list: "not overridden" must not be readable as
    # "considered and found nothing to do".
    assert "overrides" not in _package_manifest(work_dir, "basins_bravo_shud")["calibration"]


def test_declared_override_is_applied_recorded_and_never_written_to_source(tmp_path: Path) -> None:
    """#1832 spec scenario 2, all four clauses on one real publish."""
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(tmp_path / "config" / "overrides.yaml", [_declaration_entry()])
    before = _tree_digest(basins_root)

    summary, work_dir, object_root = _publish_with_declaration(
        basins_root=basins_root, tmp_path=tmp_path, declaration=declaration, run_name="declared"
    )

    assert summary["package_status_counts"] == {"published": 2}
    published = _published_calibration_bytes(
        work_dir=work_dir, object_root=object_root, model_id="basins_alpha_shud"
    ).decode("utf-8")
    # Clause 1: the declared value is what the package carries.
    assert "GEOL_DMAC\t4\n" in published
    # Clause 2: every OTHER calibration value is byte-identical to source.
    assert published == _SOURCE_CALIB_TEXT.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")
    # Clause 3: the manifest records parameter, applied value, and reason.
    overrides = _package_manifest(work_dir, "basins_alpha_shud")["calibration"]["overrides"]
    assert len(overrides) == 1
    assert overrides[0]["parameter"] == "GEOL_DMAC"
    assert overrides[0]["value"] == "4"
    assert overrides[0]["source_value"] == "5"
    assert overrides[0]["reason"] == _OVERRIDE_REASON
    assert overrides[0]["approver"] == "danker"
    # Clause 4: the Basins source tree is unwritten -- the property #1816
    # existed to protect.  Whole-tree digest, not just the one file.
    assert _tree_digest(basins_root) == before
    # The run receipt echoes the same facts for an operator reading it.
    assert [item["parameter"] for item in summary["calibration_overrides"]] == ["GEOL_DMAC"]
    assert summary["calibration_overrides_declaration"] == str(declaration)


def test_declared_override_changes_the_package_identity(tmp_path: Path) -> None:
    """#1832 spec scenario 4, end to end: a different calibration IS a different package."""
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(tmp_path / "config" / "overrides.yaml", [_declaration_entry()])

    _plain, plain_work, _plain_objects = _publish_with_declaration(
        basins_root=basins_root, tmp_path=tmp_path, declaration=None, run_name="identity-plain"
    )
    _overridden, override_work, _override_objects = _publish_with_declaration(
        basins_root=basins_root, tmp_path=tmp_path, declaration=declaration, run_name="identity-override"
    )

    plain_manifest = _package_manifest(plain_work, "basins_alpha_shud")
    override_manifest = _package_manifest(override_work, "basins_alpha_shud")
    assert plain_manifest["package_checksum"] != override_manifest["package_checksum"]
    assert plain_manifest["version"] != override_manifest["version"]
    # And the undeclared basin's identity is untouched by the other basin's override.
    assert (
        _package_manifest(plain_work, "basins_bravo_shud")["package_checksum"]
        == _package_manifest(override_work, "basins_bravo_shud")["package_checksum"]
    )


def test_declared_override_reaches_a_radiation_repaired_basin(tmp_path: Path) -> None:
    """A basin that is BOTH declared AND radiation-repaired must get both edits.

    A repaired basin enters through ``_repair_missing_radiation_contexts``, not
    through plain selection.  If override staging only walked the plainly
    selected models, this basin would publish the ORIGINAL calibration while
    the declaration claims otherwise -- exactly the silent lie design D3
    refuses.
    """
    basins_root = _write_override_fixture(tmp_path)
    (basins_root / "bravo" / "input" / "bravo" / "bravo.tsd.rl").unlink()
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="bravo")]
    )
    radiation_template = (basins_root / "alpha" / "input" / "alpha" / "alpha.tsd.rl").read_bytes()
    before = _tree_digest(basins_root)

    summary, work_dir, object_root = _publish_with_declaration(
        basins_root=basins_root, tmp_path=tmp_path, declaration=declaration, run_name="repaired"
    )

    assert summary["selected_basin_slugs"] == ["alpha", "bravo"]
    assert len(summary["repairs"]) == 1
    # Both edits landed in the same package.
    assert (
        _published_bytes_for_suffix(
            work_dir=work_dir, object_root=object_root, model_id="basins_bravo_shud", suffix=".tsd.rl"
        )
        == radiation_template
    )
    published = _published_calibration_bytes(
        work_dir=work_dir, object_root=object_root, model_id="basins_bravo_shud"
    ).decode("utf-8")
    assert published == _SOURCE_CALIB_TEXT.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")
    assert _package_manifest(work_dir, "basins_bravo_shud")["calibration"]["overrides"][0]["value"] == "4"
    assert _tree_digest(basins_root) == before


def _refused(tmp_path: Path, entries: list[dict[str, Any]], *, run_name: str) -> Any:
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(tmp_path / "config" / "overrides.yaml", entries)
    before = _tree_digest(basins_root)
    object_root = tmp_path / run_name / "objects"
    with pytest.raises(basins_calibration_overrides.CalibrationOverrideError) as excinfo:
        _publish_with_declaration(
            basins_root=basins_root, tmp_path=tmp_path, declaration=declaration, run_name=run_name
        )
    # "no package is published for that basin": the refusal lands before any
    # object is written, and the source tree is untouched either way.
    assert not list(object_root.rglob("manifest.json"))
    assert _tree_digest(basins_root) == before
    return excinfo.value


def test_declared_basin_absent_from_the_discovered_inventory_refuses(tmp_path: Path) -> None:
    """#1832 round-2 C2: a slug that exists NOWHERE in the tree is a broken deploy.

    Contract change (was: reported, not refused).  The old key could not tell a
    typo'd/renamed slug from a basin merely narrowed out of this run, so a
    declaration that will never bite again -- forever -- produced the same
    ``basin_not_in_publish_set`` line as a perfectly healthy ``--basin-slug``
    run.  After the hetianhe rollout that silence republishes the SOURCE
    ``GEOL_DMAC = 5``, re-derives the ORIGINAL `model_id` and reverts the
    registry straight back onto the NaN cliff the declaration exists to avoid.
    """
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="charlie")]
    )
    before = _tree_digest(basins_root)
    object_root = tmp_path / "absent-basin" / "objects"

    with pytest.raises(basins_calibration_overrides.CalibrationOverrideError) as excinfo:
        _publish_with_declaration(
            basins_root=basins_root,
            tmp_path=tmp_path,
            declaration=declaration,
            run_name="absent-basin",
        )

    error = excinfo.value
    assert error.error_code == "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY"
    # Names the offending entry, not just "a declaration is bad".
    assert "charlie:GEOL_DMAC" in str(error)
    assert error.details["entries"] == [
        {"basin_slug": "charlie", "parameter": "GEOL_DMAC"},
    ]
    assert error.details["basin_slugs"] == ["charlie"]
    # Fail-safe: refused before anything is written, source tree untouched.
    assert not object_root.exists() or not list(object_root.rglob("manifest.json"))
    assert _tree_digest(basins_root) == before

    # And on `--dry-run` too: the check runs before anything branches on it, so
    # a preview cannot report a run the real publish would refuse.
    with pytest.raises(basins_calibration_overrides.CalibrationOverrideError) as dry_run_info:
        registry_script.publish_all_basin_scheduler_registry(
            basins_root=basins_root,
            registry_manifest=tmp_path / "absent-basin-dry-run" / "providers" / "manifest-last.json",
            object_store_root=tmp_path / "absent-basin-dry-run" / "objects",
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "absent-basin-dry-run" / "work",
            calibration_overrides_path=declaration,
            dry_run=True,
        )
    assert dry_run_info.value.error_code == "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY"


def test_declared_basin_filtered_out_of_this_run_is_reported_not_refused(tmp_path: Path) -> None:
    """Same key, the other way in: the basin EXISTS but this run does not publish it.

    This is the `--basin-slug` case the corrected key protects.
    """
    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="bravo")]
    )

    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=tmp_path / "filtered" / "providers" / "manifest-last.json",
        object_store_root=tmp_path / "filtered" / "objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "filtered" / "work",
        basin_slugs=["alpha"],
        calibration_overrides_path=declaration,
    )

    assert summary["selected_basin_slugs"] == ["alpha"]
    assert summary["calibration_overrides"] == []
    not_applied = summary["calibration_overrides_not_applied"]
    assert [item["basin_slug"] for item in not_applied] == ["bravo"]
    # #1832 round-2 C2: distinct from the inventory-absent refusal, and a
    # distinct token from the pre-C2 `basin_not_in_publish_set`, which covered
    # BOTH cases and therefore means something different on old receipts.
    assert not_applied[0]["reason_not_applied"] == "basin_not_selected_for_this_run"


def test_checked_in_declaration_loads_without_anyone_naming_it(tmp_path: Path) -> None:
    """#1832 §1.3: no opt-in.  Both lanes load `config/calibration_overrides.yaml`.

    Nothing here names a declaration path.  Round 2 (C2) strengthened this: the
    tree now CONTAINS `hetianhe`, so default loading is proven by the override
    actually landing in the published package, not merely by the run tolerating
    a declared basin it does not have.
    """
    basins_root = _write_override_fixture(tmp_path)
    _write_basin_with_source_calibration(basins_root, "hetianhe")

    work_dir = tmp_path / "default" / "work"
    object_root = tmp_path / "default" / "objects"
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=tmp_path / "default" / "providers" / "manifest-last.json",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=work_dir,
    )

    assert summary["status"] == "published"
    assert summary["calibration_overrides_declaration"] == str(
        basins_calibration_overrides.DEFAULT_CALIBRATION_OVERRIDES_PATH
    )
    assert summary["calibration_overrides_not_applied"] == []
    assert [(item["basin_slug"], item["parameter"], item["value"]) for item in summary["calibration_overrides"]] == [
        ("hetianhe", "GEOL_DMAC", "4")
    ]
    published = _published_calibration_bytes(
        work_dir=work_dir, object_root=object_root, model_id="basins_hetianhe_shud"
    ).decode("utf-8")
    assert published == _SOURCE_CALIB_TEXT.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")
    # Nothing undeclared moved.
    for model_id in ("basins_alpha_shud", "basins_bravo_shud"):
        assert (
            _published_calibration_bytes(
                work_dir=work_dir, object_root=object_root, model_id=model_id
            ).decode("utf-8")
            == _SOURCE_CALIB_TEXT
        )


def test_declaration_naming_an_unknown_parameter_refuses_the_publish(tmp_path: Path) -> None:
    """#1832 refusal 2 -- a parameter the basin's cfg.calib does not contain."""
    error = _refused(tmp_path, [_declaration_entry(parameter="GEOL_DMACC")], run_name="unknown-parameter")

    assert error.error_code == "CALIBRATION_OVERRIDE_UNKNOWN_PARAMETER"
    assert "alpha:GEOL_DMACC" in str(error)
    assert error.details["entry"]["basin_slug"] == "alpha"
    assert error.details["calibration_file"] == "input/alpha/alpha.cfg.calib"


def test_declaration_with_an_unparseable_value_refuses_the_publish(tmp_path: Path) -> None:
    """#1832 refusal 3 -- refused before any tree is discovered or copied."""
    error = _refused(tmp_path, [_declaration_entry(value="four")], run_name="unparseable")

    assert error.error_code == "CALIBRATION_OVERRIDE_VALUE_UNPARSEABLE"
    assert "alpha:GEOL_DMAC" in str(error)
    assert error.details["declared_value"] == "'four'"


def test_declared_entry_that_matches_no_calibration_file_refuses(tmp_path: Path) -> None:
    """#1832 refusal 4 -- a declaration that applies to nothing is still a lie.

    Pinned at the application seam: a basin with no ``*.cfg.calib`` at all
    cannot pass discovery, so this is the belt-and-braces refusal that keeps
    ``apply_calibration_overrides_for_basin`` honest for any caller.
    """
    isolated_root = tmp_path / "staging"
    (isolated_root / "alpha" / "input" / "alpha").mkdir(parents=True)
    override = basins_calibration_overrides.CalibrationOverride(
        basin_slug="alpha",
        parameter="GEOL_DMAC",
        value="4",
        reason=_OVERRIDE_REASON,
        approver="danker",
        date="2026-08-24",
    )

    with pytest.raises(basins_calibration_overrides.CalibrationOverrideError) as excinfo:
        basins_calibration_overrides.apply_calibration_overrides_for_basin(
            isolated_root=isolated_root,
            basin_slug="alpha",
            overrides=[override],
        )

    assert excinfo.value.error_code == "CALIBRATION_OVERRIDE_MATCHED_NOTHING"
    assert "alpha:GEOL_DMAC" in str(excinfo.value)
    assert excinfo.value.details["calibration_file_count"] == 0


def test_checked_in_declaration_seeds_exactly_the_hetianhe_geol_dmac_entry() -> None:
    """#1832 §3.1/§3.2: the first entry, and the one deliberately NOT declared."""
    overrides = basins_calibration_overrides.load_calibration_overrides(
        Path(__file__).resolve().parents[1] / "config" / "calibration_overrides.yaml"
    )

    assert [(item.basin_slug, item.parameter, item.value) for item in overrides] == [
        ("hetianhe", "GEOL_DMAC", "4")
    ]
    # §3.2: SOIL_ALPHA is not declared for ANY basin; the source value stands.
    assert all(item.parameter != "SOIL_ALPHA" for item in overrides)
    reason = overrides[0].reason
    # The reason has to carry the measurement, not just an assertion.
    for measured in ("4.75", "4.5", "NAN", "gfs", "IFS"):
        assert measured in reason, reason


def test_refresh_lane_applies_the_same_declaration_as_the_manual_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §1.3: an override that applies on only one lane is worse than none.

    The scheduler file-provider refresh calls the same publisher.  If it
    republished a declared basin from the source value it would re-derive the
    ORIGINAL `model_id` and silently revert the registry to an identity whose
    per-model forcing and warm state have since been rebuilt under the
    overridden one -- undoing the rollout without a single error.

    Proven by call-through (the REAL publisher runs inside refresh) and by
    byte-level agreement: the two lanes must produce the same
    ``package_checksum`` for the same inputs.
    """
    from tests.test_basins_registry_import import _write_registry_fixture

    basins_root, input_dir, _inventory_path, _manifest_path, model_id = _write_registry_fixture(
        tmp_path / "fixture"
    )
    (input_dir / "alias-a.cfg.calib").write_text(_SOURCE_CALIB_TEXT, encoding="utf-8")
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="basin-a")]
    )
    expected_calibration = _SOURCE_CALIB_TEXT.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")

    # Lane A: the manual publisher.
    manual_work = tmp_path / "manual" / "work"
    manual_objects = tmp_path / "manual" / "objects"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=tmp_path / "manual" / "providers" / "manifest-last.json",
        object_store_root=manual_objects,
        object_store_prefix="s3://nhms",
        work_dir=manual_work,
        repair_missing_radiation=False,
        calibration_overrides_path=declaration,
    )
    manual_manifest = _package_manifest(manual_work, model_id)
    assert manual_manifest["calibration"]["overrides"][0]["value"] == "4"

    # Lane B: the refresh runner, driving the real publisher.  Bootstrap its
    # stores with a SOURCE-value publish first -- both because the #1080
    # cutover gate needs a previous canonical manifest, and because that is the
    # real situation: production is already carrying the un-overridden package
    # when the declaration lands.
    private_objects = tmp_path / "refresh" / "private-objects"
    shared_providers = tmp_path / "refresh" / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"
    bootstrap_work = tmp_path / "refresh" / "bootstrap-work"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        work_dir=bootstrap_work,
        repair_missing_radiation=False,
        calibration_overrides_path=None,
    )
    bootstrap_manifest = _package_manifest(bootstrap_work, model_id)
    assert "overrides" not in bootstrap_manifest["calibration"]
    assert bootstrap_manifest["package_checksum"] != manual_manifest["package_checksum"]
    readiness = shared_providers / "scheduler/canonical-readiness/index-last.json"
    state = shared_providers / "scheduler/state-index/index-last.json"
    publish_canonical_readiness_index(
        [], readiness, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    publish_state_snapshot_index(
        [], state, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    _write_current_catalogs(private_objects)
    runtime = tmp_path / "refresh" / "runtime"
    work, receipts, emergency = runtime / "work", runtime / "receipts", runtime / "emergency"
    for directory in (runtime, work, receipts, emergency):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    real_publish_all = refresh.publish_all_basin_scheduler_registry
    captured: dict[str, Any] = {}

    def _spy_publish_all(**kwargs: Any) -> dict[str, Any]:
        captured["declaration"] = kwargs.get("calibration_overrides_path")
        try:
            summary = real_publish_all(**kwargs)
        except registry_script.SchedulerRegistryPublishError as error:
            # The #1080 cutover gate runs INSIDE the publisher, after the
            # packages are written, so its refusal arrives as an exception
            # carrying the package results.  Keep them; they are the #1832
            # evidence.
            captured["failure"] = error.to_payload()
            raise
        captured["summary"] = summary
        return summary

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", _spy_publish_all)
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
            calibration_overrides_path=declaration,
        ),
        dry_run=False,
    )

    # The declaration reached the publisher through the lane's own config.
    assert captured["declaration"] == declaration
    refresh_failure = captured["failure"]
    assert refresh_failure["created_total"] == 1, refresh_failure

    # Bullet 1 + 2 together: the refresh lane minted the SAME identity the
    # manual publisher does.  The failure payload redacts manifest URIs, so
    # look the package up by the version the manual lane produced -- if refresh
    # had not loaded the declaration it would have derived the SOURCE version
    # (the bootstrap one) and this path would not exist.
    refresh_manifest_path = Path(
        private_objects, "models", model_id, str(manual_manifest["version"]), "manifest.json"
    )
    assert refresh_manifest_path.is_file(), sorted(
        path.name for path in (private_objects / "models" / model_id).iterdir()
    )
    assert manual_manifest["version"] != bootstrap_manifest["version"]
    refresh_manifest = json.loads(refresh_manifest_path.read_text(encoding="utf-8"))

    store = LocalObjectStore(private_objects, object_store_prefix="s3://nhms")
    calibration_entry = next(
        item
        for item in refresh_manifest["included_files"]
        if str(item["relative_path"]).endswith(".cfg.calib")
    )
    assert store.read_bytes(str(calibration_entry["object_uri"])).decode("utf-8") == expected_calibration
    assert refresh_manifest["calibration"]["overrides"][0]["value"] == "4"
    assert refresh_manifest["package_checksum"] == manual_manifest["package_checksum"]

    # Pinned, not swallowed: the package publishes, but swapping the canonical
    # registry onto the new identity is #1080's cutover gate, and it refuses
    # without an operator declaration.  That is orthogonal to #1832 and is a
    # constraint on the rollout, not a defect here -- an unattended refresh
    # cannot silently move the registry onto the overridden model id either.
    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    assert json.loads(registry_manifest.read_text(encoding="utf-8"))["models"][0][
        "package_checksum"
    ] == bootstrap_manifest["package_checksum"]


# ---------------------------------------------------------------------------
# #1832 round 2: the UNATTENDED lane's diagnosability.
#
# C1: `CalibrationOverrideError` is a bare `RuntimeError` subclass, so it was in
# none of the typed `except` tuples of `scheduler_file_provider_refresh` and
# landed on the generic `except Exception:` -- which writes
# `reason="provider_invalid"` and discards the error code, the message and the
# offending entry.  Nothing is logged in that file, so the fact was gone.  The
# run does not stall (nothing commits, the timer retries, the registry keeps its
# previous generation), but a bad declaration then recurs every tick under the
# same generic reason a dozen unrelated causes already emit, while the scheduler
# runs on an ever-staler registry.
#
# C2: on this lane the publisher summary is never persisted at all (no
# `output_path` is passed), so `calibration_overrides_not_applied` had zero
# persisted trace here.
# ---------------------------------------------------------------------------


def _run_refresh_lane(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    basins_root: Path,
    declaration: Path | None,
    run_name: str,
) -> tuple[dict[str, Any], Path]:
    """Drive the REAL refresh runner over ``basins_root`` with ``declaration``.

    Bootstraps the three providers from a source-value publish first -- that is
    the real situation the unattended lane runs in, and #1080's cutover gate
    needs a previous canonical registry generation to compare against.
    """
    private_objects = tmp_path / run_name / "private-objects"
    shared_providers = tmp_path / run_name / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / run_name / "bootstrap-work",
        repair_missing_radiation=False,
        calibration_overrides_path=None,
    )
    readiness = shared_providers / "scheduler/canonical-readiness/index-last.json"
    state = shared_providers / "scheduler/state-index/index-last.json"
    publish_canonical_readiness_index(
        [], readiness, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    publish_state_snapshot_index(
        [], state, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    _write_current_catalogs(private_objects)
    runtime = tmp_path / run_name / "runtime"
    work, receipts, emergency = runtime / "work", runtime / "receipts", runtime / "emergency"
    for directory in (runtime, work, receipts, emergency):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
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
            calibration_overrides_path=declaration,
        ),
        dry_run=False,
    )
    return receipt, registry_manifest


@pytest.mark.parametrize(
    ("entry", "expected_code", "expected_label"),
    [
        (
            {"basin_slug": "alpha", "parameter": "GEOL_DMACC"},
            "CALIBRATION_OVERRIDE_UNKNOWN_PARAMETER",
            "alpha:GEOL_DMACC",
        ),
        (
            {"basin_slug": "charlie", "parameter": "GEOL_DMAC"},
            "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY",
            "charlie:GEOL_DMAC",
        ),
    ],
    ids=["unknown_parameter", "basin_not_in_inventory"],
)
def test_refresh_receipt_names_the_offending_calibration_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, str],
    expected_code: str,
    expected_label: str,
) -> None:
    """#1832 round-2 C1: the unattended lane must not discard the override error."""
    import jsonschema

    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(**entry)]
    )
    receipt, registry_manifest = _run_refresh_lane(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        basins_root=basins_root,
        declaration=declaration,
        run_name=f"c1-{expected_code.lower()}",
    )

    assert receipt["outcome"] == "failed"
    # Not the generic `provider_invalid` a dozen unrelated causes emit.
    assert receipt["reason"] == "calibration_override_invalid"
    assert receipt["operation_reason"] == "calibration_override_invalid"
    block = receipt["calibration_overrides"]
    assert block["declaration_path"] == str(declaration)
    assert block["error"]["error_code"] == expected_code
    assert expected_label in block["error"]["message"]
    assert block["error"]["entries"] == [entry]

    # The receipt an operator actually reads is the one on disk, and it must
    # survive the strict schema -- a block the schema rejects would fail the
    # receipt publish and destroy the diagnosability it exists to add.
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(receipt)
    persisted = json.loads(
        (tmp_path / f"c1-{expected_code.lower()}" / "runtime" / "receipts" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["reason"] == "calibration_override_invalid"
    assert persisted["calibration_overrides"]["error"]["error_code"] == expected_code

    # Fail-safe, exactly as before: nothing committed, previous generation live.
    assert json.loads(registry_manifest.read_text(encoding="utf-8"))["models"]


def test_refresh_receipt_carries_declared_entries_that_were_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1832 round-2 C2: the unattended lane leaves a persisted trace.

    ``bravo`` IS discovered -- so the inventory-absent refusal does not fire --
    but it is unpublishable, so the run publishes without it.  That is a fact an
    operator has to be able to see: the declared override did not bite this tick.
    """
    basins_root = tmp_path / "Basins"
    _write_radiation_repair_pair(basins_root)
    for slug in ("alpha", "bravo"):
        (basins_root / slug / "input" / slug / f"{slug}.cfg.calib").write_text(
            _SOURCE_CALIB_TEXT, encoding="utf-8"
        )
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="bravo")]
    )

    receipt, _registry_manifest = _run_refresh_lane(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        basins_root=basins_root,
        declaration=declaration,
        run_name="c2-not-applied",
    )

    block = receipt["calibration_overrides"]
    assert block["declaration_path"] == str(declaration)
    assert "error" not in block
    assert block["not_applied"] == [
        {
            "basin_slug": "bravo",
            "parameter": "GEOL_DMAC",
            "reason_not_applied": "basin_not_selected_for_this_run",
        }
    ]
    persisted = json.loads(
        (tmp_path / "c2-not-applied" / "runtime" / "receipts" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["calibration_overrides"]["not_applied"] == block["not_applied"]
