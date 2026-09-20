"""Shared fixture surface of the scheduler-registry publisher suites.

Non-collectible support module (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). Every name here builds or drives
a publish: the module-wide autouse source-identity stub and its two readers, the
canonical catalog seeder, the healthy/broken/radiation basin-pair builders, the
two published-bytes readers, the fake inventory/packager/sources triple and the
calibration-declaration builders.

Only definitions used by MORE THAN ONE partition live here; a helper consumed by
a single suite stayed in that suite.

`_stub_source_identity_for_synthetic_inventories` is autouse and was module-wide
in the monolith, so every partition imports it at module scope -- that import IS
what re-registers the fixture, which is why each carries a `noqa: F401`.

Nothing here is a monkeypatch target: every `monkeypatch.setattr` in the corpus
names a PRODUCTION module (`scripts.publish_scheduler_file_registry` as
`registry_script`, `scripts.scheduler_file_provider_refresh` as `refresh`), so
design D1's repoint obligation does not arise for this split -- the patch targets
are byte-identical to the monolith's.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.common.object_store import LocalObjectStore, sha256_bytes
from tests.basins_registry_import_helpers import _make_valid_model
from workers.canonical_converter.converter import required_standard_variables_for_source

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


def _write_healthy_basin_pair(basins_root: Path) -> None:
    """Real two-basin tree, both publishable.

    Both basins carry the same ``*.tsd.lai`` header, which is what the radiation
    template matcher keys on, so alpha's ``*.tsd.rl`` is a valid template for
    bravo once bravo loses its own.
    """

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


def _package_manifest(work_dir: Path, model_id: str) -> dict[str, Any]:
    return json.loads(
        (work_dir / "package-manifests" / f"{model_id}.manifest.json").read_text(encoding="utf-8")
    )
