"""#1097: the `cutover_gate` audit block on the manifest channel.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). `publish_scheduler_registry_manifest`
refuses a malformed `cutover_gate` before it commits and leaves the previous bytes
intact; a direct publish without one omits the receipt key entirely; and an
aggregate publish without one records `not_wired` on BOTH channels.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from services.orchestrator.scheduler_file_providers import publish_scheduler_registry_manifest
from tests.provider_mode_helpers import make_directory_with_explicit_mode
from tests.publish_registry_helpers import (
    _NO_DECLARATION,
    _fake_publish_basins_package,
    _fake_sources,
    _inventory_model,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
)

# ---------------------------------------------------------------------------
# #1097: cutover_gate audit on the manifest channel
# ---------------------------------------------------------------------------


_MALFORMED_CUTOVER_GATES: list[Any] = [
    pytest.param(["enforced"], id="non_mapping"),
    pytest.param({"mode": ""}, id="empty_mode"),
    pytest.param({"mode": "gate_off"}, id="mode_outside_audited_set"),
    pytest.param(
        {"mode": "enforced", "declaration_env": 42, "declaration_present": True},
        id="non_string_declaration_env",
    ),
    pytest.param(
        {"mode": "enforced", "declaration_env": "E", "declaration_present": "no"},
        id="non_bool_declaration_present",
    ),
]


def _registry_destination(tmp_path: Path) -> Path:
    destination = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(destination.parent)
    return destination


@pytest.mark.parametrize("cutover_gate", _MALFORMED_CUTOVER_GATES)
def test_manifest_publish_refuses_malformed_cutover_gate_before_commit(
    tmp_path: Path,
    cutover_gate: Any,
) -> None:
    """#1097: the manifest channel is fail-closed on a malformed audit block.

    Before the unification it mirrored the block leniently AFTER the commit —
    an empty/unknown mode was silently rewritten to ``"not_wired"`` and the
    manifest was published anyway, so operators read contradictory audit facts
    from the companion receipt and the CLI summary.  Now the shared strict
    normalizer runs before the manifest bytes are committed.
    """
    destination = _registry_destination(tmp_path)

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        publish_scheduler_registry_manifest(
            [],
            destination,
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            cutover_gate=cutover_gate,
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert not destination.exists(), "malformed audit input must not commit a manifest"


def test_manifest_publish_leaves_previous_bytes_intact_on_malformed_cutover_gate(
    tmp_path: Path,
) -> None:
    """#1097: with a manifest already in place the refusal is a no-op — the
    previously canonical bytes stay byte-identical (spec scenario 1's
    "absent or unchanged" other half)."""
    destination = _registry_destination(tmp_path)
    previous = publish_scheduler_registry_manifest(
        [],
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        cutover_gate={
            "mode": "enforced",
            "declaration_env": registry_script.CUTOVER_DECLARATION_ENV_NAME,
            "declaration_present": True,
        },
    )
    before = destination.read_bytes()
    assert previous["cutover_gate"]["mode"] == "enforced"

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        publish_scheduler_registry_manifest(
            [],
            destination,
            object_store_root=tmp_path / "objects",
            object_store_prefix="s3://nhms",
            cutover_gate={"mode": "gate_off"},
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_CUTOVER_AUDIT_INVALID"
    assert destination.read_bytes() == before


def test_direct_manifest_publish_without_cutover_gate_omits_the_receipt_key(
    tmp_path: Path,
) -> None:
    """#1097 / spec scenario 4: the direct callers that never wire the gate
    (worker mirror, require-direct-grid, direct-grid provisioning) keep the
    pre-existing key-omitting receipt shape — ``None`` must NOT be embedded as
    a ``not_wired`` block on this entry point."""
    destination = _registry_destination(tmp_path)

    receipt = publish_scheduler_registry_manifest(
        [],
        destination,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        cutover_gate=None,
    )

    assert "cutover_gate" not in receipt, receipt
    assert destination.is_file()


def test_aggregate_publish_without_cutover_gate_records_not_wired_on_both_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1097 / spec scenario 3: the CLI aggregate entry normalizes at its own
    boundary, so an unwired run records the same ``not_wired`` block on the
    summary AND on the manifest companion receipt (unlike the direct manifest
    caller above, which omits the key)."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(inventory, Path(package_manifest_path)),
    )

    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=tmp_path / "Basins",
        registry_manifest=tmp_path / "objects/scheduler/registry/manifest-last.json",
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work",
        cutover_gate=None,
    )

    not_wired = {
        "mode": "not_wired",
        "declaration_env": None,
        "declaration_present": False,
    }
    assert summary["cutover_gate"] == not_wired
    assert summary["registry"]["cutover_gate"] == not_wired
