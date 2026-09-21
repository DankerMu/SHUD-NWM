"""The UNATTENDED refresh lane's calibration-declaration behaviour and receipts.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). The lane applies the same
declaration as the manual publisher, and the #1832 round-2 diagnosability pins:
the receipt names the offending entry instead of collapsing it into
`provider_invalid`, and it carries declared entries that were not applied.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import publish_canonical_readiness_index
from tests.basins_registry_import_helpers import _write_registry_fixture
from tests.publish_registry_helpers import (
    _SOURCE_CALIB_TEXT,
    _declaration_entry,
    _package_manifest,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
    _write_current_catalogs,
    _write_declaration,
    _write_override_fixture,
    _write_radiation_repair_pair,
)


def test_refresh_lane_applies_the_same_declaration_as_the_manual_publisher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1832 §1.3: an override that applies on only one lane is worse than none.

    The scheduler file-provider refresh calls the same publisher.  If it
    republished a declared basin from the source value it would re-derive the
    ORIGINAL `model_id` and silently revert the registry to an identity whose
    per-model forcing and warm state have since been rebuilt under the
    overridden one -- undoing the rollout without a single error.

    Proven by call-through (the REAL publisher runs inside refresh) and by
    byte-level agreement: the two lanes must produce the same
    ``package_checksum`` for the same inputs.
    """

    basins_root, input_dir, _inventory_path, _manifest_path, model_id = _write_registry_fixture(
        tmp_path / "fixture"
    )
    (input_dir / "alias-a.cfg.calib").write_text(_SOURCE_CALIB_TEXT, encoding="utf-8")
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="basin-a")]
    )
    expected_calibration = _SOURCE_CALIB_TEXT.replace("GEOL_DMAC\t5", "GEOL_DMAC\t4")

    # Lane A: the manual publisher.
    manual_work = tmp_path / "manual" / "work"
    manual_objects = tmp_path / "manual" / "objects"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=tmp_path / "manual" / "providers" / "manifest-last.json",
        object_store_root=manual_objects,
        object_store_prefix="s3://nhms",
        work_dir=manual_work,
        repair_missing_radiation=False,
        calibration_overrides_path=declaration,
    )
    manual_manifest = _package_manifest(manual_work, model_id)
    assert manual_manifest["calibration"]["overrides"][0]["value"] == "4"

    # Lane B: the refresh runner, driving the real publisher.  Bootstrap its
    # stores with a SOURCE-value publish first -- both because the #1080
    # cutover gate needs a previous canonical manifest, and because that is the
    # real situation: production is already carrying the un-overridden package
    # when the declaration lands.
    private_objects = tmp_path / "refresh" / "private-objects"
    shared_providers = tmp_path / "refresh" / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"
    bootstrap_work = tmp_path / "refresh" / "bootstrap-work"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        work_dir=bootstrap_work,
        repair_missing_radiation=False,
        calibration_overrides_path=None,
    )
    bootstrap_manifest = _package_manifest(bootstrap_work, model_id)
    assert "overrides" not in bootstrap_manifest["calibration"]
    assert bootstrap_manifest["package_checksum"] != manual_manifest["package_checksum"]
    readiness = shared_providers / "scheduler/canonical-readiness/index-last.json"
    state = shared_providers / "scheduler/state-index/index-last.json"
    publish_canonical_readiness_index(
        [], readiness, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    publish_state_snapshot_index(
        [], state, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    _write_current_catalogs(private_objects)
    runtime = tmp_path / "refresh" / "runtime"
    work, receipts, emergency = runtime / "work", runtime / "receipts", runtime / "emergency"
    for directory in (runtime, work, receipts, emergency):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    real_publish_all = refresh.publish_all_basin_scheduler_registry
    captured: dict[str, Any] = {}

    def _spy_publish_all(**kwargs: Any) -> dict[str, Any]:
        captured["declaration"] = kwargs.get("calibration_overrides_path")
        try:
            summary = real_publish_all(**kwargs)
        except registry_script.SchedulerRegistryPublishError as error:
            # The #1080 cutover gate runs INSIDE the publisher, after the
            # packages are written, so its refusal arrives as an exception
            # carrying the package results.  Keep them; they are the #1832
            # evidence.
            captured["failure"] = error.to_payload()
            raise
        captured["summary"] = summary
        return summary

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", _spy_publish_all)
    receipt = refresh.refresh_scheduler_file_providers(
        refresh.RefreshConfig(
            basins_root=basins_root,
            registry_uri=str(registry_manifest),
            readiness_uri=str(readiness),
            state_uri=str(state),
            object_store_root=private_objects,
            provider_store_root=shared_providers,
            object_store_prefix="s3://nhms",
            workspace_root=work,
            receipt_root=receipts,
            emergency_root=emergency,
            refresh_lock=runtime / "refresh.lock",
            calibration_overrides_path=declaration,
        ),
        dry_run=False,
    )

    # The declaration reached the publisher through the lane's own config.
    assert captured["declaration"] == declaration
    refresh_failure = captured["failure"]
    assert refresh_failure["created_total"] == 1, refresh_failure

    # Bullet 1 + 2 together: the refresh lane minted the SAME identity the
    # manual publisher does.  The failure payload redacts manifest URIs, so
    # look the package up by the version the manual lane produced -- if refresh
    # had not loaded the declaration it would have derived the SOURCE version
    # (the bootstrap one) and this path would not exist.
    refresh_manifest_path = Path(
        private_objects, "models", model_id, str(manual_manifest["version"]), "manifest.json"
    )
    assert refresh_manifest_path.is_file(), sorted(
        path.name for path in (private_objects / "models" / model_id).iterdir()
    )
    assert manual_manifest["version"] != bootstrap_manifest["version"]
    refresh_manifest = json.loads(refresh_manifest_path.read_text(encoding="utf-8"))

    store = LocalObjectStore(private_objects, object_store_prefix="s3://nhms")
    calibration_entry = next(
        item
        for item in refresh_manifest["included_files"]
        if str(item["relative_path"]).endswith(".cfg.calib")
    )
    assert store.read_bytes(str(calibration_entry["object_uri"])).decode("utf-8") == expected_calibration
    assert refresh_manifest["calibration"]["overrides"][0]["value"] == "4"
    assert refresh_manifest["package_checksum"] == manual_manifest["package_checksum"]

    # Pinned, not swallowed: the package publishes, but swapping the canonical
    # registry onto the new identity is #1080's cutover gate, and it refuses
    # without an operator declaration.  That is orthogonal to #1832 and is a
    # constraint on the rollout, not a defect here -- an unattended refresh
    # cannot silently move the registry onto the overridden model id either.
    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "registry_cutover_undeclared"
    assert json.loads(registry_manifest.read_text(encoding="utf-8"))["models"][0][
        "package_checksum"
    ] == bootstrap_manifest["package_checksum"]


# ---------------------------------------------------------------------------
# #1832 round 2: the UNATTENDED lane's diagnosability.
#
# C1: `CalibrationOverrideError` is a bare `RuntimeError` subclass, so it was in
# none of the typed `except` tuples of `scheduler_file_provider_refresh` and
# landed on the generic `except Exception:` -- which writes
# `reason="provider_invalid"` and discards the error code, the message and the
# offending entry.  Nothing is logged in that file, so the fact was gone.  The
# run does not stall (nothing commits, the timer retries, the registry keeps its
# previous generation), but a bad declaration then recurs every tick under the
# same generic reason a dozen unrelated causes already emit, while the scheduler
# runs on an ever-staler registry.
#
# C2: on this lane the publisher summary is never persisted at all (no
# `output_path` is passed), so `calibration_overrides_not_applied` had zero
# persisted trace here.
# ---------------------------------------------------------------------------


def _run_refresh_lane(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    basins_root: Path,
    declaration: Path | None,
    run_name: str,
) -> tuple[dict[str, Any], Path]:
    """Drive the REAL refresh runner over ``basins_root`` with ``declaration``.

    Bootstraps the three providers from a source-value publish first -- that is
    the real situation the unattended lane runs in, and #1080's cutover gate
    needs a previous canonical registry generation to compare against.
    """
    private_objects = tmp_path / run_name / "private-objects"
    shared_providers = tmp_path / run_name / "shared-providers"
    registry_manifest = shared_providers / "scheduler/registry/manifest-last.json"
    registry_script.publish_all_basin_scheduler_registry(
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=private_objects,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / run_name / "bootstrap-work",
        repair_missing_radiation=False,
        calibration_overrides_path=None,
    )
    readiness = shared_providers / "scheduler/canonical-readiness/index-last.json"
    state = shared_providers / "scheduler/state-index/index-last.json"
    publish_canonical_readiness_index(
        [], readiness, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    publish_state_snapshot_index(
        [], state, object_store_root=private_objects, object_store_prefix="s3://nhms"
    )
    _write_current_catalogs(private_objects)
    runtime = tmp_path / run_name / "runtime"
    work, receipts, emergency = runtime / "work", runtime / "receipts", runtime / "emergency"
    for directory in (runtime, work, receipts, emergency):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    receipt = refresh.refresh_scheduler_file_providers(
        refresh.RefreshConfig(
            basins_root=basins_root,
            registry_uri=str(registry_manifest),
            readiness_uri=str(readiness),
            state_uri=str(state),
            object_store_root=private_objects,
            provider_store_root=shared_providers,
            object_store_prefix="s3://nhms",
            workspace_root=work,
            receipt_root=receipts,
            emergency_root=emergency,
            refresh_lock=runtime / "refresh.lock",
            calibration_overrides_path=declaration,
        ),
        dry_run=False,
    )
    return receipt, registry_manifest


@pytest.mark.parametrize(
    ("entry", "expected_code", "expected_label"),
    [
        (
            {"basin_slug": "alpha", "parameter": "GEOL_DMACC"},
            "CALIBRATION_OVERRIDE_UNKNOWN_PARAMETER",
            "alpha:GEOL_DMACC",
        ),
        (
            {"basin_slug": "charlie", "parameter": "GEOL_DMAC"},
            "CALIBRATION_OVERRIDE_BASIN_NOT_IN_INVENTORY",
            "charlie:GEOL_DMAC",
        ),
    ],
    ids=["unknown_parameter", "basin_not_in_inventory"],
)
def test_refresh_receipt_names_the_offending_calibration_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, str],
    expected_code: str,
    expected_label: str,
) -> None:
    """#1832 round-2 C1: the unattended lane must not discard the override error."""
    import jsonschema

    basins_root = _write_override_fixture(tmp_path)
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(**entry)]
    )
    receipt, registry_manifest = _run_refresh_lane(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        basins_root=basins_root,
        declaration=declaration,
        run_name=f"c1-{expected_code.lower()}",
    )

    assert receipt["outcome"] == "failed"
    # Not the generic `provider_invalid` a dozen unrelated causes emit.
    assert receipt["reason"] == "calibration_override_invalid"
    assert receipt["operation_reason"] == "calibration_override_invalid"
    block = receipt["calibration_overrides"]
    assert block["declaration_path"] == str(declaration)
    assert block["error"]["error_code"] == expected_code
    assert expected_label in block["error"]["message"]
    assert block["error"]["entries"] == [entry]

    # The receipt an operator actually reads is the one on disk, and it must
    # survive the strict schema -- a block the schema rejects would fail the
    # receipt publish and destroy the diagnosability it exists to add.
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(receipt)
    persisted = json.loads(
        (tmp_path / f"c1-{expected_code.lower()}" / "runtime" / "receipts" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["reason"] == "calibration_override_invalid"
    assert persisted["calibration_overrides"]["error"]["error_code"] == expected_code

    # Fail-safe, exactly as before: nothing committed, previous generation live.
    assert json.loads(registry_manifest.read_text(encoding="utf-8"))["models"]


def test_refresh_receipt_carries_declared_entries_that_were_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1832 round-2 C2: the unattended lane leaves a persisted trace.

    ``bravo`` IS discovered -- so the inventory-absent refusal does not fire --
    but it is unpublishable, so the run publishes without it.  That is a fact an
    operator has to be able to see: the declared override did not bite this tick.
    """
    basins_root = tmp_path / "Basins"
    _write_radiation_repair_pair(basins_root)
    for slug in ("alpha", "bravo"):
        (basins_root / slug / "input" / slug / f"{slug}.cfg.calib").write_text(
            _SOURCE_CALIB_TEXT, encoding="utf-8"
        )
    declaration = _write_declaration(
        tmp_path / "config" / "overrides.yaml", [_declaration_entry(basin_slug="bravo")]
    )

    receipt, _registry_manifest = _run_refresh_lane(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        basins_root=basins_root,
        declaration=declaration,
        run_name="c2-not-applied",
    )

    block = receipt["calibration_overrides"]
    assert block["declaration_path"] == str(declaration)
    assert "error" not in block
    assert block["not_applied"] == [
        {
            "basin_slug": "bravo",
            "parameter": "GEOL_DMAC",
            "reason_not_applied": "basin_not_selected_for_this_run",
        }
    ]
    persisted = json.loads(
        (tmp_path / "c2-not-applied" / "runtime" / "receipts" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["calibration_overrides"]["not_applied"] == block["not_applied"]
