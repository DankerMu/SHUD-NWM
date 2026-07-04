from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed
from workers.model_registry.basins_soil_alpha_repair import repair_soil_alpha_calibration_for_basin


def test_package_version_for_nested_basin_is_safe_and_content_stable() -> None:
    model = _inventory_model("zhaochen/BST", shud_input_name="BST")

    first = registry_script.package_version_for_model(model)
    second = registry_script.package_version_for_model(dict(model))

    assert first == second
    assert first.startswith("vbasins-zhaochen_bst-")
    assert "/" not in first


def test_package_version_changes_when_source_identity_moves() -> None:
    old_model = _inventory_model("kashigeer")
    new_model = dict(old_model)
    new_model["source_path"] = "/volume/nwm/Basins/kashigeer"
    new_model["resolved_source_path"] = "/volume/nwm/Basins/kashigeer"
    new_model["input_dir"] = "/volume/nwm/Basins/kashigeer/input/kashigeer"

    assert registry_script.package_version_for_model(old_model) != registry_script.package_version_for_model(new_model)


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
    assert rows["basins_qhh_shud"]["resource_profile"]["lineage"] == "basins_scheduler_file_registry"
    assert rows["basins_zhaochen_bst_shud"]["resource_profile"]["project_name"] == "BST"
    assert rows["basins_zhaochen_bst_shud"]["output_segment_count"] == 7


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
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
    )

    assert summary["selected_basin_slugs"] == ["qhh", "tailanhe"]
    assert len(summary["repairs"]) == 1
    assert summary["repairs"][0]["basin_slug"] == "tailanhe"
    assert summary["repair_staging_cleanup"]["status"] == "cleaned"
    assert summary["repair_staging_cleanup"]["removed"][0]["name"] == "repaired-basins"
    assert not (tmp_path / "work" / "repaired-basins").exists()
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
    summary = registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
        retain_repair_staging=True,
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
