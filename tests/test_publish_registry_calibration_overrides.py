"""Calibration: nothing is clamped, and only a DECLARED override is applied.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). The #1816 half -- an
out-of-bounds SOIL_ALPHA or GEOL_DMAC is published byte-identically and no repair
is recorded -- and the whole #1832 declared-override channel: what an override
changes (bytes, summary, package identity, a radiation-repaired basin), what it
never touches (the Basins source tree), the four refusal shapes, and the exact
content of the checked-in `config/calibration_overrides.yaml`.

The UNATTENDED lane's view of the same declaration is
`tests/test_publish_registry_refresh_lane.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
import workers.model_registry.basins_calibration_overrides as basins_calibration_overrides
from packages.common.object_store import sha256_bytes
from tests.basins_registry_import_helpers import _make_valid_model, _write_registry_fixture
from tests.publish_registry_helpers import (
    _NO_DECLARATION,
    _OVERRIDE_REASON,
    _SOURCE_CALIB_TEXT,
    _declaration_entry,
    _package_manifest,
    _published_bytes_for_suffix,
    _published_calibration_bytes,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
    _write_declaration,
    _write_override_fixture,
    _write_soil_alpha_model_files,
)


def _write_out_of_bounds_calibration_fixture(tmp_path: Path, *, parameter: str) -> tuple[Path, Path, str]:
    """A real publishable basin whose ``cfg.calib`` sits outside the deleted bounds.

    ``SHUD_SOIL_ALPHA_MAX = 20.0`` / ``SHUD_GEOL_DMAC_MAX = 4.0`` had no source
    anywhere in the repository (#1816); the calibrations they overrode were
    produced by external users running SHUD to convergence.  Publication must
    now copy them through untouched.
    """


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


def _replace_calibration_parameter(text: str, parameter: str, value: str) -> str:
    """Rewrite exactly one ``<parameter><TAB><value>`` line, preserving the rest.

    Refuses a declaration that names no fixture parameter: the expected text
    would otherwise silently equal the source, making the byte comparison
    vacuous for that declaration.
    """
    lines = text.splitlines(keepends=True)
    matched = [index for index, line in enumerate(lines) if line.startswith(f"{parameter}\t")]
    assert len(matched) == 1, (
        f"fixture calibration must contain exactly one '{parameter}' line, found {len(matched)}"
    )
    index = matched[0]
    lines[index] = f"{parameter}\t{value}\n"
    return "".join(lines)


def _write_basin_with_source_calibration(basins_root: Path, slug: str) -> None:
    """One more publishable basin carrying ``_SOURCE_CALIB_TEXT``."""

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

    Nothing here names a declaration path.  The fixture derives every basin the
    checked-in declaration names (via `load_calibration_overrides`) and asserts
    every declared parameter lands in that basin's published bytes, so a
    legitimate declaration addition cannot stale this fixture the way the six
    HHe entries did.
    """
    declared = basins_calibration_overrides.load_calibration_overrides(
        basins_calibration_overrides.DEFAULT_CALIBRATION_OVERRIDES_PATH
    )
    declared_by_basin: dict[str, list[Any]] = {}
    for override in declared:
        declared_by_basin.setdefault(override.basin_slug, []).append(override)
    assert declared_by_basin, "checked-in declaration must name at least one basin"

    basins_root = _write_override_fixture(tmp_path)
    for slug in declared_by_basin:
        _write_basin_with_source_calibration(basins_root, slug)

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
    # Every declared entry was applied; summary covers all declared basins.
    assert len(summary["calibration_overrides"]) == len(declared)
    assert {(item["basin_slug"], item["parameter"]) for item in summary["calibration_overrides"]} == {
        (override.basin_slug, override.parameter) for override in declared
    }
    for slug, basin_overrides in declared_by_basin.items():
        model_id = registry_script._slug_id(slug)
        published = _published_calibration_bytes(
            work_dir=work_dir, object_root=object_root, model_id=f"basins_{model_id}_shud"
        ).decode("utf-8")
        expected = _SOURCE_CALIB_TEXT
        for override in basin_overrides:
            expected = _replace_calibration_parameter(expected, override.parameter, override.value)
        assert published == expected, slug
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


def test_checked_in_declaration_seeds_exactly_one_entry() -> None:
    """#1832 §3.1: the current checked-in declaration is exactly this one entry.

    hetianhe's GEOL_DMAC=4 is the only entry.  The six HHe GEOL_KSATH=2.0
    entries were retired on 2026-09-14 (#1904): the owner confirmed the override
    was not needed, and node-22's active HHe packages already run the delivered
    calibration.  This exact tuple is the deliberate review gate for
    declaration-content changes: the default-load fixture above derives its
    basins/expected bytes from `load_calibration_overrides`, so an accidental
    slug or value change cannot hide behind a fixture that builds itself.
    """
    overrides = basins_calibration_overrides.load_calibration_overrides(
        Path(__file__).resolve().parents[1] / "config" / "calibration_overrides.yaml"
    )

    assert [(item.basin_slug, item.parameter, item.value) for item in overrides] == [
        ("hetianhe", "GEOL_DMAC", "4"),
    ]
    # §3.2: SOIL_ALPHA is not declared for ANY basin; the source value stands.
    assert all(item.parameter != "SOIL_ALPHA" for item in overrides)
    # The hetianhe reason has to carry the measurement, not just an assertion.
    hetianhe = next(item for item in overrides if item.basin_slug == "hetianhe")
    for measured in ("4.75", "4.5", "NAN", "gfs", "IFS"):
        assert measured in hetianhe.reason, hetianhe.reason
