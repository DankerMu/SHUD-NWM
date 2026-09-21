"""Missing-radiation-template repair: its budget, its reach and its blast radius.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). The direct
`repair_missing_tsd_rl_for_basin` contract (copies inside the private root, and
refuses before creating the target when the budget is exceeded), the bulk
publisher's use of it, the two unsalvageable-model paths, the run-scoped
workspace reuse of an already repaired package, and the proof that supplying a
template touches no calibration byte.

The refusals a repaired-but-still-unpublishable model raises against the cutover
gate live in `tests/test_publish_registry_skip_refusals.py`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from scripts import scheduler_file_provider_refresh as refresh
from tests.basins_registry_import_helpers import _write_registry_fixture
from tests.publish_registry_helpers import (
    _NO_DECLARATION,
    _fake_publish_basins_package,
    _fake_sources,
    _inventory_from_file,
    _inventory_model,
    _published_bytes_for_suffix,
    _published_calibration_bytes,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
    _write_healthy_basin_pair,
    _write_radiation_repair_pair,
    _write_soil_alpha_model_files,
)
from workers.model_registry.basins_radiation_template import repair_missing_tsd_rl_for_basin, repair_performed


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


def test_repaired_package_is_reused_across_run_scoped_workspaces(tmp_path: Path) -> None:

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
