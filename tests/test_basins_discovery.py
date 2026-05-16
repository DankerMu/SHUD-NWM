from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from workers.model_registry.basins_discovery import BasinsDiscoveryError, discover_basins_inventory
from workers.model_registry.cli import _argparse_main


def test_missing_root_cli_returns_stable_error_and_no_inventory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "inventory.json"
    missing = tmp_path / "missing-root"

    exit_code = _argparse_main(["discover-basins", "--basins-root", str(missing), "--output", str(output)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "BASINS_ROOT_NOT_FOUND" in captured.err
    assert not output.exists()


def test_cli_root_precedes_env_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    make_valid_model(root_a / "a", "a")
    make_valid_model(root_b / "b", "alias")
    output = tmp_path / "inventory.json"
    monkeypatch.setenv("NHMS_BASINS_ROOT", str(root_a))

    exit_code = _argparse_main(["discover-basins", "--basins-root", str(root_b), "--output", str(output)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["model_count"] == 1
    inventory = json.loads(output.read_text(encoding="utf-8"))
    assert inventory["root"] == str(root_b)
    assert [(model["basin_slug"], model["shud_input_name"]) for model in inventory["models"]] == [("b", "alias")]


def test_symlink_root_records_source_fields(tmp_path: Path) -> None:
    real_root = tmp_path / "real-basins"
    make_valid_model(real_root / "qhh", "qhh")
    linked_root = tmp_path / "linked-basins"
    linked_root.symlink_to(real_root, target_is_directory=True)

    inventory = discover_basins_inventory(linked_root)

    assert inventory["source_is_symlink"] is True
    assert inventory["resolved_root"] == str(real_root.resolve())
    model = one_model(inventory)
    assert model["source_path"] == str(linked_root / "qhh")
    assert model["resolved_source_path"] == str((real_root / "qhh").resolve())


def test_valid_minimal_model_tree_inventory_fields(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "basin-a", "alias-a", calibration_count=1, forcing_count=1)

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "valid"
    assert model["basin_slug"] == "basin-a"
    assert model["shud_input_name"] == "alias-a"
    assert model["model_id"] == "basins_basin_a_shud"
    assert model["suggested_ids"]["model_id"] == "basins_basin_a_shud"
    assert model["forcing_dir_original_name"] == "forcing"
    assert model["calibration_count"] == 1
    assert model["forcing_csv_count"] == 1
    assert model["missing_required_files"] == []
    assert model["generated_sidecar_count"] == 0
    assert model["default_import_eligible"] is True
    assert model["checksums"]


def test_partial_missing_tsd_rl_and_legacy_focing(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "tailanhe", "tlh", include_tsd_rl=False, forcing_dir_name="focing", forcing_count=1)

    model = one_model(discover_basins_inventory(root))

    assert model["basin_slug"] == "tailanhe"
    assert model["shud_input_name"] == "tlh"
    assert model["status"] == "partial"
    assert "*.tsd.rl" in model["missing_required_files"]
    assert "legacy_focing_dir" in model["quirks"]
    assert model["forcing_dir_original_name"] == "focing"
    assert model["default_import_eligible"] is False
    assert model["default_publish_eligible"] is False


def test_sidecar_recursion_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "qhh"
    input_dir = make_valid_model(model_dir, "qhh", forcing_count=1)
    (input_dir / ".DS_Store").write_text("ignored\n", encoding="utf-8")
    ea_dir = input_dir / "@eaDir"
    ea_dir.mkdir()
    (ea_dir / "qhh.cfg.para@SynoEAStream").write_text("ignored\n", encoding="utf-8")
    gis_ea = input_dir / "gis" / "@eaDir"
    gis_ea.mkdir()
    (gis_ea / "domain.shp@SynoEAStream").write_text("ignored\n", encoding="utf-8")
    forcing_ea = model_dir / "forcing" / "@eaDir"
    forcing_ea.mkdir()
    (forcing_ea / "X1.csv@SynoEAStream").write_text("ignored\n", encoding="utf-8")

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "valid"
    assert model["forcing_csv_count"] == 1
    assert model["calibration_count"] == 0
    assert model["generated_sidecar_count"] == 4
    assert "generated_sidecars_ignored" in model["quirks"]
    assert all("@eaDir" not in name for names in model["required_files"].values() for name in names)
    assert all("@SynoEAStream" not in name for name in model["checksums"])


def test_forcing_focing_conflict_prefers_canonical_with_warning(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "conflict"
    make_valid_model(model_dir, "conflict", forcing_count=1)
    focing = model_dir / "focing"
    focing.mkdir()
    (focing / "X2.csv").write_text("time,value\n", encoding="utf-8")

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert model["status"] == "valid"
    assert model["forcing_dir_original_name"] == "forcing"
    assert model["forcing_csv_count"] == 1
    assert "forcing_dir_conflict" in model["quirks"]
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_FORCING_DIR_CONFLICT"]


def test_symlink_escape_model_is_skipped_with_warning(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    outside = tmp_path / "outside"
    make_valid_model(outside / "escape", "escape")
    (root).mkdir()
    (root / "escape-link").symlink_to(outside / "escape", target_is_directory=True)

    inventory = discover_basins_inventory(root)

    assert inventory["models"] == []
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]


def test_nested_zhaochen_style_models_are_discovered(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "zhaochen" / "WEM", "WEM")
    make_valid_model(root / "qhh", "qhh")

    inventory = discover_basins_inventory(root)

    assert [model["basin_slug"] for model in inventory["models"]] == ["qhh", "zhaochen/WEM"]
    assert [model["model_id"] for model in inventory["models"]] == ["basins_qhh_shud", "basins_zhaochen_wem_shud"]


def test_bounded_large_forcing_directory_counts_csv_without_checksums(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "large", "large", forcing_count=10_000)

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "valid"
    assert model["forcing_csv_count"] == 10_000
    assert all(not name.endswith(".csv") for name in model["checksums"])


def test_unreadable_root_and_subdir_when_permissions_enforced(tmp_path: Path) -> None:
    unreadable_root = tmp_path / "unreadable-root"
    unreadable_root.mkdir()
    unreadable_root.chmod(0)
    try:
        with pytest.raises(BasinsDiscoveryError) as exc_info:
            discover_basins_inventory(unreadable_root)
        assert exc_info.value.error_code == "BASINS_ROOT_UNREADABLE"
    finally:
        unreadable_root.chmod(0o700)

    root = tmp_path / "basins"
    locked_model = root / "locked-model"
    locked_model.mkdir(parents=True)
    locked_model.chmod(0)
    try:
        with pytest.raises(BasinsDiscoveryError) as exc_info:
            discover_basins_inventory(root)
        assert exc_info.value.error_code == "BASINS_DIRECTORY_UNREADABLE"
    finally:
        locked_model.chmod(0o700)


@pytest.mark.skipif(
    os.getenv("NHMS_RUN_BASINS_SMOKE") != "1" or not Path("data/Basins").exists(),
    reason="real Basins smoke is opt-in and requires data/Basins",
)
def test_real_basins_smoke_inventory_contract() -> None:
    inventory = discover_basins_inventory(Path("data/Basins"))

    assert inventory["model_count"] == 13
    slugs = {model["basin_slug"] for model in inventory["models"]}
    assert {
        "qhh",
        "heihe",
        "kashigeer",
        "weiganhe",
        "xinanjiang_upstream",
        "hetianhe",
        "qinyijiang",
        "keliya",
        "tailanhe",
        "zhaochen/WEM",
        "zhaochen/HHY",
        "zhaochen/MC",
        "zhaochen/BST",
    } == slugs
    by_slug = {model["basin_slug"]: model for model in inventory["models"]}
    assert by_slug["tailanhe"]["status"] == "partial"
    assert "legacy_focing_dir" in by_slug["tailanhe"]["quirks"]
    assert by_slug["kashigeer"]["shud_input_name"] == "ksge"
    assert by_slug["qinyijiang"]["shud_input_name"] == "nanlin"
    assert by_slug["xinanjiang_upstream"]["shud_input_name"] == "xinanjiang"


def make_valid_model(
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
        "cfg.ic",
        "cfg.calib",
        "sp.mesh",
        "sp.riv",
        "sp.rivseg",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
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
            (forcing_dir / f"X{index + 1:06d}.csv").write_text("time,value\n", encoding="utf-8")

    return input_dir


def one_model(inventory: dict[str, object]) -> dict[str, object]:
    models = inventory["models"]
    assert isinstance(models, list)
    assert len(models) == 1
    model = models[0]
    assert isinstance(model, dict)
    return model
