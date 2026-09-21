"""Package identity and the publisher's per-context budget / orphan accounting.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). The `package_version_for_model`
template contract, the import-context limit and its release-before-next-context
guarantee, the four partial-failure reports (import, resource, canonical
preimage, post-manifest orphan), the orphan-sample slice, the two bulk-publish
happy paths and the real end-to-end refresh that keeps packages private while
the canonical manifest stays shared.
"""
from __future__ import annotations

import json
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    FileSchedulerModelRegistry,
    publish_canonical_readiness_index,
)
from tests.basins_registry_import_helpers import _write_registry_fixture
from tests.publish_registry_helpers import (
    _NO_DECLARATION,
    _fake_publish_basins_package,
    _fake_sources,
    _inventory_from_file,
    _inventory_model,
    _source_identity,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
    _write_current_catalogs,
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
