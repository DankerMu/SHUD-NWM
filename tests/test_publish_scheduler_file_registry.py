from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script


def test_package_version_for_nested_basin_is_safe_and_content_stable() -> None:
    model = _inventory_model("zhaochen/BST", shud_input_name="BST")

    first = registry_script.package_version_for_model(model)
    second = registry_script.package_version_for_model(dict(model))

    assert first == second
    assert first.startswith("vbasins-zhaochen_bst-")
    assert "/" not in first


def test_package_version_template_rejects_unsafe_path_segment() -> None:
    with pytest.raises(registry_script.SchedulerRegistryPublishError) as exc_info:
        registry_script.package_version_for_model(
            _inventory_model("qhh"),
            template="vbasins/{slug_id}",
        )

    assert exc_info.value.error_code == "SCHEDULER_REGISTRY_PACKAGE_VERSION_UNSAFE"


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
    assert rows["basins_qhh_shud"]["frequency_capabilities"] == {"return_periods": False}
    assert rows["basins_qhh_shud"]["resource_profile"]["lineage"] == "basins_scheduler_file_registry"
    assert rows["basins_zhaochen_bst_shud"]["resource_profile"]["project_name"] == "BST"
    assert rows["basins_zhaochen_bst_shud"]["output_segment_count"] == 7


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


def _fake_publish_basins_package(
    *,
    inventory_path: str | Path,
    model_id: str,
    version: str,
    output_path: str | Path,
    copy_forcing: bool,
    object_store: Any,
) -> dict[str, Any]:
    del inventory_path, copy_forcing
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
    object_store.write_bytes_atomic(manifest_key, content)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
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
