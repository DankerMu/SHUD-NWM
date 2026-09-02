"""Basins package forcing-identity invariants for excluded and mutated payloads.

Partition 6 of 6 of the former monolith ``tests/test_basins_package_publication.py``
(issue #1912).  Shared test support lives in the non-collectible
``tests/basins_package_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import workers.model_registry.basins_package as basins_package
from tests.basins_package_helpers import _make_valid_model, _object_store_env, _publish_identity_snapshot
from workers.model_registry.basins_discovery import discover_basins_inventory, write_inventory


def test_emptying_excluded_forcing_directory_does_not_move_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1813 task 4.1: #1702 item 3's cleanup must be a true no-op for package identity.

    Driven through real re-discovery rather than a hand-edited inventory, so any
    forcing-derived inventory field is caught mechanically.
    """

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=4, calibration_count=1)
    forcing_dir = root / "basin-a" / "forcing"

    before = _publish_identity_snapshot(root, tmp_path, monkeypatch, "before")

    for path in sorted(forcing_dir.glob("*.csv")):
        path.unlink()
    assert forcing_dir.is_dir()
    assert not list(forcing_dir.iterdir())

    after = _publish_identity_snapshot(root, tmp_path, monkeypatch, "after")

    assert after == before


def test_emptied_forcing_republishes_the_same_immutable_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production-shaped form of task 4.1: same store, same version, no checksum conflict."""

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=4, calibration_count=1)
    forcing_dir = root / "basin-a" / "forcing"
    _object_store_env(tmp_path, monkeypatch)

    def publish(label: str) -> dict[str, object]:
        inventory = discover_basins_inventory(root)
        inventory_path = tmp_path / f"inventory-{label}.json"
        write_inventory(inventory, inventory_path)
        return basins_package.publish_basins_package(
            inventory_path=inventory_path,
            model_id=inventory["models"][0]["model_id"],
            version="vbasins-test",
            output_path=tmp_path / f"manifest-{label}.json",
        )

    first = publish("first")
    for path in sorted(forcing_dir.glob("*.csv")):
        path.unlink()
    second = publish("second")

    assert first["status"] == "published"
    assert second["status"] == "already_done"
    assert second["package_checksum"] == first["package_checksum"]


def test_excluded_forcing_payload_changes_do_not_move_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1813 task 4.2: mutating excluded forcing CSV bytes in place moves nothing."""

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=4, calibration_count=1)
    forcing_dir = root / "basin-a" / "forcing"

    before = _publish_identity_snapshot(root, tmp_path, monkeypatch, "before")

    for path in sorted(forcing_dir.glob("*.csv")):
        path.write_text("time,value\n2026-01-01,999\n2026-01-02,1000\n", encoding="utf-8")

    after = _publish_identity_snapshot(root, tmp_path, monkeypatch, "after")

    assert after == before


def test_removing_the_forcing_directory_outright_is_a_structural_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1813 task 4.3 negative control.

    Emptying the directory is a payload cleanup; removing it is a structural
    source change and must stay visible.  It surfaces through the raw inventory
    hash, which the cutover gate treats as a nested identity field.
    """

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=4, calibration_count=1)
    forcing_dir = root / "basin-a" / "forcing"

    before = _publish_identity_snapshot(root, tmp_path, monkeypatch, "before")

    for path in sorted(forcing_dir.glob("*.csv")):
        path.unlink()
    forcing_dir.rmdir()

    after = _publish_identity_snapshot(root, tmp_path, monkeypatch, "after")

    assert after["source_inventory_checksum"] != before["source_inventory_checksum"]
    # The package content itself is unchanged, which is exactly the
    # discrimination this change buys: payload cleanup is invisible, the
    # structural fact is not.
    assert after["content_sha256"] == before["content_sha256"]
    assert after["source_sha256"] == before["source_sha256"]
    assert after["package_checksum"] == before["package_checksum"]


def test_copied_forcing_payload_bytes_still_bind_to_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1813 task 1.6: copy_forcing=True loses no identity coverage.

    Copied payloads are ordinary `included_files` entries with role=forcing, so
    their sha256 is covered by the package checksum material directly.
    """

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=2, calibration_count=1)
    forcing_dir = root / "basin-a" / "forcing"

    before = _publish_identity_snapshot(root, tmp_path, monkeypatch, "before", copy_forcing=True)

    (forcing_dir / "X000001.csv").write_text("time,value\n2026-01-01,999\n", encoding="utf-8")

    after = _publish_identity_snapshot(root, tmp_path, monkeypatch, "after", copy_forcing=True)

    # `basins_package_source_identity` plans the production (excluded) shape, so
    # only the published package checksum is expected to move here.
    assert after["package_checksum"] != before["package_checksum"]
    assert after["content_sha256"] == before["content_sha256"]
    manifest = json.loads((tmp_path / "manifest-after.json").read_text(encoding="utf-8"))
    forcing_entries = [entry for entry in manifest["included_files"] if entry["role"] == "forcing"]
    assert len(forcing_entries) == 2


def test_renaming_the_legacy_focing_directory_is_a_structural_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1813 task 4.3, second leg of the spec's structural scenario.

    The legacy `focing/` spelling is a source fact, not payload evidence: the
    packager resolves the forcing source directory from it, so a rename must
    stay visible even though the CSVs behind it never move identity.
    """

    root = tmp_path / "basins"
    _make_valid_model(root / "basin-a", "alias-a", forcing_count=3, calibration_count=1, forcing_dir_name="focing")

    before = _publish_identity_snapshot(root, tmp_path, monkeypatch, "before")

    (root / "basin-a" / "focing").rename(root / "basin-a" / "forcing")

    after = _publish_identity_snapshot(root, tmp_path, monkeypatch, "after")

    assert after["source_inventory_checksum"] != before["source_inventory_checksum"]
    assert after["content_sha256"] == before["content_sha256"]
    assert after["source_sha256"] == before["source_sha256"]
    assert after["package_checksum"] == before["package_checksum"]
