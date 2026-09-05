"""Basins registry import: CLI database gate, JSON reporting and public paths.

Partition 3 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 5 argparse/click public-path contracts that need no real
database.  Shared test support lives in the non-collectible
``tests/basins_registry_import_helpers.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.basins_registry_import_helpers import (
    _CLI_MODEL_ADMIN_AUTH_ARGS,
    _invoke_click,
    _package_manifest_for_model,
    _write_registry_fixture,
    _write_river_shapefile,
)
from workers.model_registry.basins_discovery import (
    discover_basins_inventory,
    write_inventory,
)
from workers.model_registry.cli import _argparse_main


def test_import_command_requires_database_but_consumes_manifests_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_DATABASE_URL_MISSING"
    assert error["model_id"] == model_id


def test_import_command_reports_missing_sidecar_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    (input_dir / "gis" / "domain.shx").unlink()

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
    assert error["model_id"] == model_id
    assert error["missing_sidecar"] == "gis/domain.shx"


def test_import_command_reports_missing_gis_directory_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    shutil.rmtree(input_dir / "gis")

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert error["error_code"] == "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
    assert error["model_id"] == model_id
    assert error["missing_sidecar"] == "gis/domain.shp"


def test_import_command_reports_river_shp_invariant_violation_before_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PR 2 (spec "river.shp single-part invariant"): when the river.shp
    record count diverges from .sp.riv reach count, ingestion fails fast
    with ``BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED`` before any DB
    write. Previously this was ``BASINS_REGISTRY_SEGMENT_COUNT_MISMATCH``
    keyed on .sp.rivseg, which is no longer the geometry oracle."""

    _, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    # Shrink river.shp to 1 record so it no longer matches the 2-reach
    # .sp.riv header. _write_river_shapefile writes both .shp/.shx/.dbf
    # in place; the manifest checksums for these files have been recorded
    # so we have to drop the source-identity guard by also rebuilding the
    # manifest entries -- here we sidestep it by leaving the inventory
    # alone and relying on the run-time parser check (the file checksum
    # check happens via _validate_manifest_included_files; we use a NEW
    # fixture with a custom sp_river_count that doesn't match the river.shp
    # record count instead).
    _, fresh_input_dir, fresh_inventory_path, fresh_manifest_path, fresh_model_id = (
        _write_registry_fixture(tmp_path / "second", sp_river_count=1, sp_segment_count=2)
    )
    # _write_registry_fixture above wrote river.shp with reach_count=1 to
    # match sp_river_count=1; explicitly overwrite to a 3-record river.shp
    # WITHOUT touching the inventory so the parser sees a mismatch.
    _write_river_shapefile(fresh_input_dir / "gis" / "river", reach_count=3)
    # Rebuild inventory/manifest checksums for the resized river.shp.
    inventory = discover_basins_inventory(tmp_path / "second" / "basins")
    write_inventory(inventory, fresh_inventory_path)
    fresh_model = inventory["models"][0]
    refreshed_manifest = _package_manifest_for_model(
        fresh_model, fresh_model["model_id"], inventory=inventory
    )
    fresh_manifest_path.write_text(
        json.dumps(refreshed_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    exit_code = _argparse_main(
        [
            "import-basins-registry",
            "--inventory",
            str(fresh_inventory_path),
            "--package-manifest",
            str(fresh_manifest_path),
            "--database-url",
            "postgresql://nhms:nhms@localhost:1/nhms",
            *_CLI_MODEL_ADMIN_AUTH_ARGS,
        ]
    )

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert exit_code == 1
    assert captured.out == ""
    assert error["error_code"] == "BASINS_REGISTRY_RIVER_SHP_INVARIANT_VIOLATED"
    assert error["model_id"] == fresh_model_id
    assert error["river_shp_record_count"] == 3
    assert error["sp_riv_count"] == 1
    del input_dir, inventory_path, manifest_path, model_id


def test_click_path_exposes_import_basins_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, inventory_path, manifest_path, _ = _write_registry_fixture(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    exit_code = _invoke_click(
        [
            "import-basins-registry",
            "--inventory",
            str(inventory_path),
            "--package-manifest",
            str(manifest_path),
        ]
    )

    assert exit_code == 1
