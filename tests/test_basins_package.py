"""#1832: the package manifest is where a calibration override is recorded.

#1816's most damning measurement was that of eight silently rewritten packages
exactly one publish receipt survived, in a scratch directory: the receipt lives
in the publisher workspace, the package itself said nothing.  These tests pin
the seam that fixes that -- ``publish_basins_package`` carries the applied
overrides into ``manifest["calibration"]["overrides"]``, which travels with the
bytes -- and the absence rule that keeps "not overridden" readable.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.provision_direct_grid_scheduler_registry as direct_grid
import scripts.publish_scheduler_file_registry as scheduler_registry
import workers.model_registry.basins_package as basins_package
from packages.common.object_store import LocalObjectStore
from tests.test_basins_package_publication import _object_store_env, _write_valid_inventory
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory

_HETIANHE_OVERRIDE = {
    "basin_slug": "basin-a",
    "parameter": "GEOL_DMAC",
    "value": "4",
    "source_value": "5",
    "reason": "GEOL_DMAC 5 and 4.75 both produce NaN / EXIT 10; 4.5 and 4 run clean.",
    "approver": "danker",
    "date": "2026-08-24",
    "relative_path": "input/alias-a/alias-a.cfg.calib",
    "sha256": "0" * 64,
}


def _publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_name: str,
    calibration_text: str | None = None,
    calibration_overrides: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], Path]:
    run_root = tmp_path / run_name
    inventory_path, model_id = _write_valid_inventory(run_root, calibration_count=1)
    if calibration_text is not None:
        calib = run_root / "basins" / "basin-a" / "input" / "alias-a" / "alias-a.cfg.calib"
        calib.write_text(calibration_text, encoding="utf-8")
        # Re-discover so the inventory's checksums match the edited bytes, the
        # same way the publisher re-discovers from its staging copy.
        write_inventory(discover_basins_inventory(run_root / "basins"), inventory_path)
    _object_store_env(run_root, monkeypatch)
    model = json.loads(inventory_path.read_text(encoding="utf-8"))["models"][0]
    identity = basins_package.basins_package_source_identity(
        inventory_path=inventory_path,
        model_id=model_id,
    )
    kwargs: dict[str, object] = {}
    if calibration_overrides is not None:
        kwargs["calibration_overrides"] = calibration_overrides
    basins_package.publish_basins_package(
        inventory_path=inventory_path,
        model_id=model_id,
        version=scheduler_registry.package_version_for_model(model, source_identity=identity),
        output_path=run_root / "manifest.json",
        **kwargs,
    )
    return (
        json.loads((run_root / "manifest.json").read_text(encoding="utf-8")),
        run_root / "object-store",
    )


def test_package_without_declared_overrides_carries_no_override_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §2.4: absence is meaningful, so there must be no key at all."""
    manifest, _object_root = _publish(tmp_path, monkeypatch, run_name="plain")

    calibration = manifest["calibration"]
    assert "overrides" not in calibration, calibration
    # Guard against a vacuous pass: the calibration block itself is present.
    assert calibration["included_count"] >= 1


def test_empty_override_list_is_not_recorded_as_an_empty_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list must be indistinguishable from "nothing was declared".

    ``[]`` in the manifest would read as "the publisher considered overrides
    and applied none", which is a claim this publisher is not entitled to make
    for a basin it never staged.
    """
    manifest, _object_root = _publish(tmp_path, monkeypatch, run_name="empty", calibration_overrides=[])

    assert "overrides" not in manifest["calibration"]


def test_manifest_records_parameter_value_and_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §2.3 / spec scenario 2, third clause."""
    manifest, object_root = _publish(
        tmp_path,
        monkeypatch,
        run_name="declared",
        calibration_text="GEOL_KSATH\t0.00977999747288218\nGEOL_DMAC\t4\nSOIL_ALPHA\t8.19327372615961\n",
        calibration_overrides=[_HETIANHE_OVERRIDE],
    )

    overrides = manifest["calibration"]["overrides"]
    assert len(overrides) == 1
    recorded = overrides[0]
    assert recorded["parameter"] == "GEOL_DMAC"
    assert recorded["value"] == "4"
    assert "NaN" in recorded["reason"]
    assert recorded["source_value"] == "5"
    # The record must be in the STORED manifest, not just the local receipt:
    # the stored copy is the one that travels with the package.
    store = LocalObjectStore(object_root, object_store_prefix="s3://nhms")
    stored = json.loads(store.read_bytes(str(manifest["manifest_uri"])).decode("utf-8"))
    assert stored["calibration"]["overrides"] == overrides


def test_applying_an_override_re_derives_the_package_and_model_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec scenario 4: the same package with and without the override differs.

    That is the intended cost (design D4), not a defect: a package with a
    different calibration IS a different package, and the ``dg_*`` model id is
    seeded from ``package_checksum``.
    """
    source_text = "GEOL_KSATH\t0.00977999747288218\nGEOL_DMAC\t5\nSOIL_ALPHA\t8.19327372615961\n"
    overridden_text = source_text.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")

    plain, _plain_root = _publish(
        tmp_path, monkeypatch, run_name="identity-source", calibration_text=source_text
    )
    overridden, _overridden_root = _publish(
        tmp_path,
        monkeypatch,
        run_name="identity-override",
        calibration_text=overridden_text,
        calibration_overrides=[_HETIANHE_OVERRIDE],
    )

    assert plain["package_checksum"] != overridden["package_checksum"]
    assert plain["version"] != overridden["version"]

    snapshot = SimpleNamespace(grid_id="gfs-0p25", grid_signature="sig")
    identities = {
        direct_grid._package_identity(
            {"model_id": manifest["model_id"], "package_checksum": manifest["package_checksum"]},
            "gfs",
            snapshot,
        )
        for manifest in (plain, overridden)
    }
    assert len(identities) == 2, identities
