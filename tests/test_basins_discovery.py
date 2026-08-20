from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from workers.model_registry import basins_discovery
from workers.model_registry.basins_discovery import BasinsDiscoveryError, _walk_files, discover_basins_inventory
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


def test_empty_inventory_is_not_importable(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    root.mkdir()

    inventory = discover_basins_inventory(root)

    assert inventory["models"] == []
    assert inventory["model_count"] == 0
    assert inventory["importable"] is False


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
    assert inventory["importable"] is False
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]


def test_symlinked_input_alias_outside_root_is_not_read_or_importable(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "alias-escape"
    input_parent = model_dir / "input"
    input_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    external_input = make_valid_model(outside / "external", "external")
    (input_parent / "external").symlink_to(external_input, target_is_directory=True)

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert inventory["importable"] is False
    assert model["status"] == "partial"
    assert model["shud_input_name"] == ""
    assert model["required_files"]["cfg_para"] == []
    assert model["checksums"] == {}
    assert model["default_import_eligible"] is False
    assert "unsafe_symlink_outside_root" in model["quirks"]
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]
    assert inventory["warnings"][0]["path"] == str(input_parent / "external")


def test_symlink_loop_descendant_is_skipped_with_stable_warning(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "loop"
    make_valid_model(model_dir, "loop")
    loop = model_dir / "forcing"
    try:
        loop.symlink_to(loop)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert inventory["importable"] is False
    assert model["status"] == "partial"
    assert model["default_import_eligible"] is False
    assert "unsafe_symlink_outside_root" in model["quirks"]
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_UNRESOLVABLE"]
    assert inventory["warnings"][0]["path"] == str(loop)


def test_dangling_forcing_symlink_inside_root_is_not_reported_unresolvable(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "dangling"
    make_valid_model(model_dir, "dangling")
    dangling = model_dir / "forcing"
    try:
        dangling.symlink_to(root / "missing-forcing-target", target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert [warning["code"] for warning in inventory["warnings"]] == []
    assert inventory["importable"] is True
    assert model["status"] == "valid"
    assert model["default_import_eligible"] is True
    assert model["forcing_dir"] is None
    assert model["forcing_csv_count"] == 0


def test_symlink_loop_behind_missing_component_is_treated_as_nonexistence(tmp_path: Path) -> None:
    # The strict walk aborts with ENOENT on the missing `gone` component, while
    # the non-strict fallback collapses `..` and lands on a real symlink loop.
    # That combination must classify as nonexistence (silent skip) on every
    # supported CPython, never as an exception and never as UNRESOLVABLE.
    root = tmp_path / "basins"
    model_dir = root / "loop-behind-missing"
    make_valid_model(model_dir, "loop-behind-missing")
    loop_dir = model_dir / "loopdir"
    forcing = model_dir / "forcing"
    try:
        loop_dir.symlink_to(loop_dir)
        forcing.symlink_to(Path("gone") / ".." / "loopdir", target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert [warning["code"] for warning in inventory["warnings"]] == []
    assert inventory["importable"] is True
    assert model["status"] == "valid"
    assert model["default_import_eligible"] is True
    assert model["forcing_dir"] is None
    assert model["forcing_csv_count"] == 0


def test_dangling_forcing_symlink_outside_root_is_reported_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "dangling-escape"
    make_valid_model(model_dir, "dangling-escape")
    outside_missing = tmp_path / "outside" / "missing-forcing-target"
    dangling = model_dir / "forcing"
    try:
        dangling.symlink_to(outside_missing, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]
    assert inventory["warnings"][0]["path"] == str(dangling)
    assert inventory["importable"] is False
    assert model["status"] == "partial"
    assert model["default_import_eligible"] is False
    assert "unsafe_symlink_outside_root" in model["quirks"]
    assert model["forcing_dir"] is None
    assert model["forcing_csv_count"] == 0


def test_symlinked_forcing_outside_root_is_not_counted_or_importable(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    model_dir = root / "forcing-escape"
    make_valid_model(model_dir, "forcing-escape")
    outside = tmp_path / "outside"
    external_forcing = outside / "forcing"
    external_forcing.mkdir(parents=True)
    (external_forcing / "X000001.csv").write_text("time,value\n", encoding="utf-8")
    (model_dir / "forcing").symlink_to(external_forcing, target_is_directory=True)

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert inventory["importable"] is False
    assert model["status"] == "partial"
    assert model["forcing_dir"] is None
    assert model["forcing_csv_count"] == 0
    assert model["default_import_eligible"] is False
    assert "unsafe_symlink_outside_root" in model["quirks"]
    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]
    assert inventory["warnings"][0]["path"] == str(model_dir / "forcing")


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


def test_walk_files_streams_paths_without_returning_list(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    root.mkdir()
    (root / "a.csv").write_text("time,value\n", encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("value\n", encoding="utf-8")

    traversal = _walk_files(root, root.resolve(), [])

    assert not isinstance(traversal, list)
    assert iter(traversal) is traversal
    assert sorted(path.name for path in traversal) == ["a.csv", "b.txt"]


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


#: Native SHUD IC header, matching :data:`VALID_MESH_HEADER`'s element count:
#: ``<mesh> <mesh-state-columns> <minute-time>``. Real node-27 baselines all carry
#: this three-token shape (task 0(f) probe, 13/13 models).
VALID_IC_HEADER = "484\t6\t38920320.000000"
#: Native SHUD mesh header: ``<n_elements> <n_columns>``.
VALID_MESH_HEADER = "484\t8"


# ---------------------------------------------------------------------------
# IC header content-shape gate (#1197). Existence + checksum were all discovery
# ever checked, so a present, non-empty, checksum-clean `23106\t6` IC sailed
# through registration and only detonated in the first real SHUD run.
# ---------------------------------------------------------------------------

INCIDENT_IC_HEADER = "23106\t6"


def test_two_token_ic_header_is_rejected_and_other_models_still_register(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(
        root / "lh-gl",
        "LH-GL",
        ic_header=INCIDENT_IC_HEADER,
        mesh_header="23106\t8",
    )
    make_valid_model(root / "keliya", "keliya")

    inventory = discover_basins_inventory(root)
    by_slug = {model["basin_slug"]: model for model in inventory["models"]}

    bad = by_slug["lh-gl"]
    assert bad["status"] == "partial"
    assert bad["default_import_eligible"] is False
    assert bad["default_publish_eligible"] is False
    # The shape verdict must not leak into the missing-file set: its consumers
    # compare that set for an exact `{"*.tsd.rl"}` repairability match.
    assert bad["missing_required_files"] == []
    assert len(bad["invalid_required_files"]) == 1
    reason = bad["invalid_required_files"][0]
    assert reason.startswith("LH-GL.cfg.ic:")
    assert "2 numeric token(s)" in reason
    # Discovery is not aborted: the well-formed sibling still registers.
    assert by_slug["keliya"]["status"] == "valid"
    assert by_slug["keliya"]["invalid_required_files"] == []
    assert inventory["model_count"] == 2


def test_ic_mesh_count_mismatch_against_sp_mesh_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(
        root / "basin-a",
        "alias-a",
        ic_header="484\t6\t38920320.000000",
        mesh_header="6335\t8",
    )

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "partial"
    reason = model["invalid_required_files"][0]
    # Both counts named so the operator can see which side is wrong.
    assert "484" in reason and "6335" in reason


def test_three_and_four_token_ic_headers_with_matching_mesh_still_register(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "native", "native", ic_header="484\t6\t38920320.000000")
    make_valid_model(root / "lake", "lake", ic_header="484\t50\t3\t38920320.000000")

    inventory = discover_basins_inventory(root)

    for model in inventory["models"]:
        assert model["status"] == "valid", model["invalid_required_files"]
        assert model["invalid_required_files"] == []
        assert model["default_publish_eligible"] is True


def test_unparseable_sp_mesh_header_rejects_the_model_naming_the_mesh_file(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "basin-a", "alias-a", mesh_header="ID\tNode1")

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "partial"
    reasons = model["invalid_required_files"]
    assert any(reason.startswith("alias-a.sp.mesh:") for reason in reasons)


def test_unreadable_ic_blocks_registration_with_a_reason_distinct_from_a_shape_violation(
    tmp_path: Path,
) -> None:
    shaped_root = tmp_path / "shaped"
    make_valid_model(shaped_root / "basin-a", "alias-a", ic_header=INCIDENT_IC_HEADER)
    shape_reason = one_model(discover_basins_inventory(shaped_root))["invalid_required_files"][0]

    root = tmp_path / "basins"
    input_dir = make_valid_model(root / "basin-a", "alias-a")
    locked = input_dir / "alias-a.cfg.ic"
    locked.chmod(0)
    try:
        model = one_model(discover_basins_inventory(root))
    finally:
        locked.chmod(0o600)

    assert model["status"] == "partial"
    # Matched by glob, so it is NOT missing -- it is unreadable.
    assert model["missing_required_files"] == []
    unreadable_reason = model["invalid_required_files"][0]
    assert unreadable_reason.startswith("alias-a.cfg.ic:")
    assert "could not be read" in unreadable_reason
    # AC-4: the two refusal channels must not be reported as one another.
    assert unreadable_reason != shape_reason
    assert "numeric token(s)" not in unreadable_reason
    assert "could not be read" not in shape_reason


def test_every_matched_ic_is_validated_not_just_the_first(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    input_dir = make_valid_model(root / "basin-a", "alias-a")
    # A second matched IC, malformed. Sorted after the well-formed one, so a
    # first-match-only check would pass the model.
    (input_dir / "zz-second.cfg.ic").write_text(f"{INCIDENT_IC_HEADER}\n", encoding="utf-8")

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "partial"
    assert [reason.split(":")[0] for reason in model["invalid_required_files"]] == ["zz-second.cfg.ic"]


def test_multiple_matched_sp_mesh_files_are_rejected_as_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    input_dir = make_valid_model(root / "basin-a", "alias-a")
    (input_dir / "zz-second.sp.mesh").write_text("484\t8\n", encoding="utf-8")

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "partial"
    reasons = model["invalid_required_files"]
    assert len(reasons) == 1
    ambiguous = reasons[0]
    assert "2 *.sp.mesh files" in ambiguous
    assert "alias-a.sp.mesh" in ambiguous and "zz-second.sp.mesh" in ambiguous
    # Ambiguity is its own reason, not a shape verdict on the (well-formed) IC.
    assert "numeric token(s)" not in ambiguous


# ---------------------------------------------------------------------------
# Unreadable required files (#1430). The checksum walk used to `continue` past an
# OSError: a required file that was matched by glob but could not be stat'ed or
# hashed left no checksum entry, no quirk and no warning, so "present but
# unreadable" registered as a healthy model.
# ---------------------------------------------------------------------------


def test_checksum_walk_marks_a_required_file_unreadable_when_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stat failure is injected INSIDE the walk, never around discovery.

    ``Path.stat`` cannot be patched across a whole ``discover_basins_inventory``
    run: on 3.11 ``Path.is_file()`` goes through ``self.stat()`` and does not
    swallow EACCES, so the fake escapes at ``_glob_non_sidecar_files`` and aborts
    discovery, while on 3.14 ``is_file()`` reaches ``os.path.isfile`` and misses
    the patch entirely -- green locally, red on CI, pinning nothing either way.
    Driving the walk directly keeps the injection to the one ``path.stat()`` the
    size check makes. The discovery-level consequences (partial, quirk, payload
    key) are pinned by the ``_sha256`` injection below, which needs no patching
    of pathlib at all.
    """

    root = tmp_path / "basins"
    input_dir = root / "basin-a" / "input" / "alias-a"
    input_dir.mkdir(parents=True)
    unreadable_name = "alias-a.tsd.lai"
    (input_dir / unreadable_name).write_text("tsd.lai\n", encoding="utf-8")
    readable = input_dir / "alias-a.tsd.mf"
    readable.write_text("tsd.mf\n", encoding="utf-8")
    expected_checksum = hashlib.sha256(readable.read_bytes()).hexdigest()
    resolved_root = root.resolve()
    warnings: list[basins_discovery.DiscoveryWarning] = []
    real_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.name == unreadable_name:
            raise PermissionError(errno.EACCES, "simulated stat failure")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", fake_stat)
        checksums, unreadable = basins_discovery._checksums_for_required_files(
            input_dir,
            {"tsd_lai": [unreadable_name], "tsd_mf": [readable.name]},
            resolved_root,
            warnings,
        )

    assert [reason.split(":")[0] for reason in unreadable] == [unreadable_name]
    assert [(warning.code, warning.path) for warning in warnings] == [
        ("BASINS_REQUIRED_FILE_UNREADABLE", str(input_dir / unreadable_name))
    ]
    # The rest of the walk is unaffected: only the unreadable file lost its checksum.
    assert checksums == {readable.name: expected_checksum}


def test_unreadable_required_file_degrades_status_to_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "basins"
    make_valid_model(root / "basin-a", "alias-a")
    unreadable_name = "alias-a.tsd.lai"
    real_sha256 = basins_discovery._sha256

    def fake_sha256(path: Path) -> str:
        if path.name == unreadable_name:
            raise OSError(errno.EIO, "simulated hash failure")
        return real_sha256(path)

    monkeypatch.setattr(basins_discovery, "_sha256", fake_sha256)

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert model["status"] == "partial"
    assert model["default_import_eligible"] is False
    assert model["default_publish_eligible"] is False
    # Matched by glob, so it is NOT missing -- and not a content-shape verdict either.
    assert model["missing_required_files"] == []
    assert model["invalid_required_files"] == []
    assert [reason.split(":")[0] for reason in model["unreadable_required_files"]] == [unreadable_name]
    assert "unreadable_required_file" in model["quirks"]
    assert "unsafe_symlink_outside_root" not in model["quirks"]
    assert [
        warning["path"] for warning in inventory["warnings"] if warning["code"] == "BASINS_REQUIRED_FILE_UNREADABLE"
    ] == [str(Path(model["input_dir"]) / unreadable_name)]
    # The rest of the walk is unaffected: only the unreadable file lost its checksum.
    assert unreadable_name not in model["checksums"]
    assert model["checksums"]


def test_checksum_walk_skips_root_escaping_files_without_calling_them_unreadable(tmp_path: Path) -> None:
    """The unsafe-symlink skip is its own arm inside the checksum walk.

    Driven directly, because discovery never gets an escaping path this far:
    ``_match_required_files`` already drops it, so a discovery-level fixture
    would leave the walk's resolve-None ``continue`` unexecuted and would stay
    green even if that arm were folded into the unreadable verdict. Here the
    walk is handed a ``required_files`` mapping that names the escaping file, so
    the arm runs: no checksum entry AND no unreadable entry.
    """

    root = tmp_path / "basins"
    input_dir = root / "basin-a" / "input" / "alias-a"
    input_dir.mkdir(parents=True)
    readable = input_dir / "alias-a.tsd.mf"
    readable.write_text("tsd.mf\n", encoding="utf-8")
    outside = tmp_path / "outside" / "alias-a.cfg.para"
    outside.parent.mkdir(parents=True)
    outside.write_text("cfg.para\n", encoding="utf-8")
    escaping = input_dir / "alias-a.cfg.para"
    try:
        escaping.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink support unavailable: {error}")

    warnings: list[basins_discovery.DiscoveryWarning] = []
    checksums, unreadable = basins_discovery._checksums_for_required_files(
        input_dir,
        {"cfg_para": ["alias-a.cfg.para"], "tsd_mf": ["alias-a.tsd.mf"]},
        root.resolve(),
        warnings,
    )

    assert unreadable == []
    assert [warning.code for warning in warnings] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]
    assert [warning.path for warning in warnings] == [str(escaping)]
    # The escaping file is skipped; the rest of the walk is untouched.
    assert checksums == {"alias-a.tsd.mf": hashlib.sha256(readable.read_bytes()).hexdigest()}


def test_required_file_escaping_the_root_stays_on_the_symlink_channel(tmp_path: Path) -> None:
    """Payload-level observation: an escaping required file reads as missing, not unreadable.

    It never reaches the checksum walk (``_match_required_files`` drops it), so
    this pins what an operator sees, not the walk's own symlink arm — that arm is
    pinned by ``test_checksum_walk_skips_root_escaping_files_without_calling_them_unreadable``.
    """

    root = tmp_path / "basins"
    input_dir = make_valid_model(root / "basin-a", "alias-a")
    outside = tmp_path / "outside" / "domain.shp"
    outside.parent.mkdir(parents=True)
    outside.write_text("domain.shp\n", encoding="utf-8")
    escaping = input_dir / "gis" / "domain.shp"
    escaping.unlink()
    escaping.symlink_to(outside)

    inventory = discover_basins_inventory(root)
    model = one_model(inventory)

    assert [warning["code"] for warning in inventory["warnings"]] == ["BASINS_SYMLINK_OUTSIDE_ROOT"]
    assert model["unreadable_required_files"] == []
    assert "unreadable_required_file" not in model["quirks"]
    assert model["missing_required_files"] == ["gis/domain.shp"]
    assert "unsafe_symlink_outside_root" in model["quirks"]


def test_readable_required_files_keep_valid_status_and_checksum_shape(tmp_path: Path) -> None:
    root = tmp_path / "basins"
    input_dir = make_valid_model(root / "basin-a", "alias-a")

    model = one_model(discover_basins_inventory(root))

    assert model["status"] == "valid"
    assert model["unreadable_required_files"] == []
    assert "unreadable_required_file" not in model["quirks"]
    expected_checksums = {
        relative_name: hashlib.sha256((input_dir / relative_name).read_bytes()).hexdigest()
        for matches in model["required_files"].values()
        for relative_name in matches
    }
    assert model["checksums"] == expected_checksums


def make_valid_model(
    model_dir: Path,
    input_name: str,
    *,
    include_tsd_rl: bool = True,
    calibration_count: int = 0,
    forcing_count: int = 0,
    forcing_dir_name: str = "forcing",
    ic_header: str = VALID_IC_HEADER,
    mesh_header: str = VALID_MESH_HEADER,
) -> Path:
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.calib",
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
    # These two carry REAL headers: discovery validates the IC header's numeric-token
    # shape against the mesh element count, so a placeholder body would make every
    # "valid model" fixture invalid.
    (input_dir / f"{input_name}.cfg.ic").write_text(f"{ic_header}\n1\t0.1\n", encoding="utf-8")
    (input_dir / f"{input_name}.sp.mesh").write_text(f"{mesh_header}\nID\tNode1\n", encoding="utf-8")
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
