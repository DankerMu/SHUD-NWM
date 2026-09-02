"""Shared, non-collectible support for the Basins package publication suites.

Owns the helper closure (baseline lines 3242-3417 of the former monolith
``tests/test_basins_package_publication.py``) and the Basins-package import surface
its six publication partitions and ``tests/test_basins_package.py`` share (issue
#1912).  Defines no ``test_*`` callable and collects zero nodes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory
from workers.model_registry.cli import _click_main


def _write_valid_inventory(
    tmp_path: Path,
    *,
    forcing_count: int = 0,
    calibration_count: int = 0,
    basin_slug: str = "basin-a",
    input_name: str = "alias-a",
) -> tuple[Path, str]:
    root = tmp_path / "basins"
    _make_valid_model(
        root / basin_slug,
        input_name,
        forcing_count=forcing_count,
        calibration_count=calibration_count,
    )
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    return inventory_path, inventory["models"][0]["model_id"]


def _object_store_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    return object_root


def _required_files_for_input_name(input_name: str) -> dict[str, list[str]]:
    required = {
        "cfg_para": [f"{input_name}.cfg.para"],
        "cfg_ic": [f"{input_name}.cfg.ic"],
        "cfg_calib": [f"{input_name}.cfg.calib"],
        "sp_mesh": [f"{input_name}.sp.mesh"],
        "sp_riv": [f"{input_name}.sp.riv"],
        "sp_rivseg": [f"{input_name}.sp.rivseg"],
        "sp_att": [f"{input_name}.sp.att"],
        "para_soil": [f"{input_name}.para.soil"],
        "para_geol": [f"{input_name}.para.geol"],
        "para_lc": [f"{input_name}.para.lc"],
        "tsd_forc": [f"{input_name}.tsd.forc"],
        "tsd_lai": [f"{input_name}.tsd.lai"],
        "tsd_mf": [f"{input_name}.tsd.mf"],
        "tsd_rl": [f"{input_name}.tsd.rl"],
    }
    for layer in ("domain", "river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            required[f"gis_{layer}_{suffix}"] = [f"gis/{layer}.{suffix}"]
    return required


def _one_entry(manifest: dict[str, object], role: str) -> dict[str, object]:
    entries = [
        entry
        for entry in manifest["included_files"]  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("role") == role
    ]
    assert len(entries) == 1
    return entries[0]


def _manifest_payload_checksum(manifest: dict[str, object]) -> str:
    payload = dict(manifest)
    payload["included_files"] = [
        entry
        for entry in manifest["included_files"]  # type: ignore[index]
        if isinstance(entry, dict) and entry.get("role") != "manifest"
    ]
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return hashlib.sha256(content).hexdigest()


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1


def _make_valid_model(
    model_dir: Path,
    input_name: str,
    *,
    include_tsd_rl: bool = True,
    calibration_count: int = 0,
    forcing_count: int = 0,
    forcing_dir_name: str = "forcing",
) -> Path:
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.calib",
        "sp.riv",
        "sp.rivseg",
        "sp.att",
        "lake.bathy",
        "lake.ic",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    # Real SHUD headers: discovery validates the IC header's numeric-token shape
    # against the mesh element count, so placeholder bodies would make every model
    # here invalid (#1197).
    (input_dir / f"{input_name}.cfg.ic").write_text(
        "484\t6\t38920320.000000\n1\t0.1\n", encoding="utf-8"
    )
    (input_dir / f"{input_name}.sp.mesh").write_text("484\t8\nID\tNode1\n", encoding="utf-8")
    (input_dir / f"{input_name}.lake.sp").write_text("lake.sp\n", encoding="utf-8")
    if include_tsd_rl:
        (input_dir / f"{input_name}.tsd.rl").write_text("radiation\n", encoding="utf-8")

    gis_dir = input_dir / "gis"
    gis_dir.mkdir()
    for layer in ("domain", "river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            (gis_dir / f"{layer}.{suffix}").write_text(f"{layer}.{suffix}\n", encoding="utf-8")

    if calibration_count:
        calib_dir = model_dir / "CALIB"
        calib_dir.mkdir()
        for index in range(calibration_count):
            (calib_dir / f"top{index + 1:02d}.calib").write_text("calib\n", encoding="utf-8")

    if forcing_count:
        forcing_dir = model_dir / forcing_dir_name
        forcing_dir.mkdir()
        for index in range(forcing_count):
            (forcing_dir / f"X{index + 1:06d}.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")

    return input_dir


def _publish_identity_snapshot(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    *,
    copy_forcing: bool = False,
) -> dict[str, str]:
    """Re-discover `root` and publish it into its own object store, returning the four identity values."""

    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / f"inventory-{label}.json"
    write_inventory(inventory, inventory_path)
    model_id = inventory["models"][0]["model_id"]
    source_identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / f"object-store-{label}"))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    manifest_path = tmp_path / f"manifest-{label}.json"
    result = basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version="vbasins-identity",
        output_path=manifest_path,
        copy_forcing=copy_forcing,
    )
    assert result["status"] == "published"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "content_sha256": source_identity["content_sha256"],
        "source_sha256": source_identity["source_sha256"],
        "package_checksum": manifest["package_checksum"],
        "source_inventory_checksum": manifest["source_inventory_checksum"],
    }
