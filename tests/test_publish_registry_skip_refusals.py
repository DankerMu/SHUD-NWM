"""What the #1080 cutover gate does with a model the publisher skipped.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). A bulk skip of an
already-registered model is a refusal; the refusal carries the unreadable file's
name and the skip-cause evidence; and a DECLARED retirement is the one shape that
lets the refresh publish without the skipped model.
"""
from __future__ import annotations

import errno
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from packages.common.object_store import sha256_bytes
from scripts import scheduler_file_provider_refresh as refresh
from tests.publish_registry_helpers import (
    _NO_DECLARATION,
    _break_bravo_beyond_repair,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
    _write_healthy_basin_pair,
)


def test_bulk_skip_of_an_already_registered_model_is_refused_by_the_cutover_gate(
    tmp_path: Path,
) -> None:
    """The bulk skip is NOT a licence to drop a model that is already published.

    The two tests above cover the bootstrap shape (nothing registered yet), where
    skipping bravo is the whole point.  Production's refresh lane runs the same
    bulk publish behind #1080's registry-cutover gate, and there the skip means
    something else: the canonical registry HAS ``basins_bravo_shud``, the
    prospective one does not, so the gate classifies it as a removal and refuses
    the whole canonical replacement (``registry_cutover_removal_refused``).  The
    refresh lane has no bypass for that — only the manual CLI's
    ``--allow-uncovered-cutover``.  So the skip's real terminal state, once a
    basin has ever been published, is a failed run with the previous registry
    intact, and this test pins that rather than the comment's word for it.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    first = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    assert first["status"] == "published"
    previous_bytes = registry_manifest.read_bytes()
    assert {row["model_id"] for row in json.loads(previous_bytes)["models"]} == {
        "basins_alpha_shud",
        "basins_bravo_shud",
    }

    _break_bravo_beyond_repair(basins_root)

    # Wire the real #1080 gate exactly as scheduler_file_provider_refresh does:
    # the previous canonical bytes/digest snapshot taken before the run, no
    # cutover declaration, real publish.
    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=None,
            dry_run=False,
            classification_sink=classification.update,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-refresh",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
        )

    assert excinfo.value.error_code == "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED"
    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"
    assert excinfo.value.details["provider_phase"] == "precommit"
    assert classification["removed"]["items"] == ["basins_bravo_shud"]
    assert [entry["model_id"] for entry in classification["refused"]["items"]] == ["basins_bravo_shud"]
    assert {entry["reason"] for entry in classification["refused"]["items"]} == {
        "registry_cutover_removal_refused"
    }
    # Canonical registry survives untouched: alpha is not republished alone.
    assert registry_manifest.read_bytes() == previous_bytes


def test_unreadable_required_file_skip_carries_the_file_name_into_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1553: an unreadable required file is named under ``unreadable_required_files``.

    ``basins_discovery`` matches ``bravo.tsd.lai`` (so ``missing_required_files``
    stays empty) but the strict resolution denies the read, so the model is
    ``partial`` with ONLY the unreadable cause.  The bulk publish skips it, the
    cutover gate refuses the removal, and the ``registry_cutover_removal_refused``
    entry carries the exact file name under ``unreadable_required_files`` instead
    of a bare ``status=partial`` with empty cause lists.
    """

    realpath = os.path.realpath
    target_name = "bravo.tsd.lai"
    denied = False

    def denied_realpath(path: str, *, strict: bool = False) -> str:
        if denied and strict and str(path).endswith(target_name):
            raise PermissionError(errno.EACCES, "simulated denied traversal")
        return realpath(path, strict=strict)

    monkeypatch.setattr(registry_script.os.path, "realpath", denied_realpath)

    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    previous_bytes = registry_manifest.read_bytes()
    denied = True
    assert {row["model_id"] for row in json.loads(previous_bytes)["models"]} == {
        "basins_alpha_shud",
        "basins_bravo_shud",
    }

    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}
    skipped_models: dict[str, Mapping[str, Any]] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=None,
            dry_run=False,
            classification_sink=classification.update,
            now=generated_at,
            skipped_models=skipped_models,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-refresh",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
            skipped_model_sink=skipped_models.update,
        )

    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"
    assert registry_manifest.read_bytes() == previous_bytes
    refusal = classification["refused"]["items"][0]
    assert refusal["model_id"] == "basins_bravo_shud"
    assert refusal["reason"] == "registry_cutover_removal_refused"
    assert refusal["status"] == "partial"
    # The unreadable cause is carried, not silently folded into a bare partial.
    assert refusal["missing_required_files"] == []
    assert refusal["invalid_required_files"] == []
    assert [reason.split(":")[0] for reason in refusal["unreadable_required_files"]] == [
        "bravo.tsd.lai"
    ]


def test_undeclared_removal_refusal_carries_the_skip_cause_evidence(
    tmp_path: Path,
) -> None:
    """#1433 branch (a): the refusal above stays fail-closed AND says why.

    Same bulk skip, same refusal, previous canonical bytes intact, nothing
    published — the only addition is that the refusal entry now carries the
    inventory row the publisher skipped on (``status`` /
    ``missing_required_files`` / ``invalid_required_files``), so an operator can
    tell an invalid package from a deleted model directory without digging
    through a workspace the refresh lane deletes.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    previous_bytes = registry_manifest.read_bytes()

    _break_bravo_beyond_repair(basins_root)

    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}
    skipped_models: dict[str, Mapping[str, Any]] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=None,
            dry_run=False,
            classification_sink=classification.update,
            now=generated_at,
            skipped_models=skipped_models,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-refresh",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
            skipped_model_sink=skipped_models.update,
        )

    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"
    assert registry_manifest.read_bytes() == previous_bytes
    assert classification["declared_retirements"]["total"] == 0
    refusal = classification["refused"]["items"][0]
    assert refusal["model_id"] == "basins_bravo_shud"
    assert refusal["reason"] == "registry_cutover_removal_refused"
    assert refusal["status"] == "partial"
    # The row is the POST-repair one: the radiation repair restored bravo's
    # ``*.tsd.rl`` from alpha's template, so what is left is what actually
    # blocks the publish — the malformed IC header.
    assert refusal["missing_required_files"] == []
    assert any("2 numeric token(s)" in reason for reason in refusal["invalid_required_files"])


def test_declared_retirement_lets_the_refresh_publish_without_the_skipped_model(
    tmp_path: Path,
) -> None:
    """#1433: the retire declaration is the way out of the removal deadlock.

    Same tree, same gate wiring, and the same bulk skip as the refusal test
    above.  The only difference is a declaration naming ``basins_bravo_shud``
    with ``transition_mode: "retire"`` and ``new_checksum: null``: the gate then
    admits the removal, the refresh publishes, canonical loses exactly bravo's
    row, and alpha publishes normally.

    The first (refused) run is the runbook's step 2, executed the way an
    operator executes it: a real refresh that fails closed, whose receipt
    carries BOTH values the declaration needs — the generation
    (``registry_classification.generation``) and the previous canonical
    ``old_checksum`` (on the removal refusal row).  The test reads them from the
    classification exactly as the runbook says to, so a regression that stops
    publishing the generation reddens here instead of stranding an operator.
    """
    basins_root = tmp_path / "Basins"
    _write_healthy_basin_pair(basins_root)
    registry_manifest = tmp_path / "providers" / "scheduler" / "registry" / "manifest-last.json"
    object_store_root = tmp_path / "objects"

    registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-bootstrap",
    )
    previous_bytes = registry_manifest.read_bytes()
    previous_rows = {row["model_id"]: row for row in json.loads(previous_bytes)["models"]}
    assert set(previous_rows) == {"basins_alpha_shud", "basins_bravo_shud"}

    _break_bravo_beyond_repair(basins_root)

    generated_at = datetime(2026, 8, 16, 0, 0, tzinfo=UTC)
    classification: dict[str, Any] = {}
    declaration_env: dict[str, str] = {}

    def precommit_provider_generation(
        workspace: Path,
        packages: Sequence[Mapping[str, Any]],
        registry_models: Sequence[Mapping[str, Any]],
    ) -> None:
        refresh._registry_precommit_gate(
            workspace,
            packages,
            registry_models,
            previous_registry_bytes=previous_bytes,
            previous_registry_sha256=sha256_bytes(previous_bytes),
            prospective_generated_at=generated_at,
            cutover_declaration_env=declaration_env.get("path"),
            dry_run=False,
            classification_sink=classification.update,
            now=generated_at,
        )

    with pytest.raises(registry_script.SchedulerRegistryPublishError) as excinfo:
        registry_script.publish_all_basin_scheduler_registry(
            calibration_overrides_path=_NO_DECLARATION,
            basins_root=basins_root,
            registry_manifest=registry_manifest,
            object_store_root=object_store_root,
            object_store_prefix="s3://nhms",
            work_dir=tmp_path / "work-undeclared",
            registry_generated_at=generated_at,
            precommit_validator=precommit_provider_generation,
        )
    assert excinfo.value.details["provider_reason"] == "registry_cutover_removal_refused"

    # Step 2 of the runbook: both declaration inputs come off THIS receipt.
    refused_generation = classification["generation"]
    assert refused_generation is not None
    refused_removal = next(
        item
        for item in classification["refused"]["items"]
        if item["reason"] == "registry_cutover_removal_refused"
    )
    assert refused_removal["old_checksum"] == previous_rows["basins_bravo_shud"]["package_checksum"]

    declaration = tmp_path / "retire-declaration.json"
    declaration.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-08-16T00:00:00Z",
                "generation": refused_generation,
                "entries": [
                    {
                        "model_id": "basins_bravo_shud",
                        "old_checksum": refused_removal["old_checksum"],
                        "new_checksum": None,
                        "effective_cycle_utc": "2026-08-16T12:00:00Z",
                        "transition_mode": "retire",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    declaration_env["path"] = str(declaration)

    summary = registry_script.publish_all_basin_scheduler_registry(
        calibration_overrides_path=_NO_DECLARATION,
        basins_root=basins_root,
        registry_manifest=registry_manifest,
        object_store_root=object_store_root,
        object_store_prefix="s3://nhms",
        work_dir=tmp_path / "work-retire",
        registry_generated_at=generated_at,
        precommit_validator=precommit_provider_generation,
    )

    assert summary["status"] == "published"
    published_rows = json.loads(registry_manifest.read_text(encoding="utf-8"))["models"]
    assert {row["model_id"] for row in published_rows} == {"basins_alpha_shud"}
    assert classification["removed"]["items"] == ["basins_bravo_shud"]
    assert classification["refused"]["total"] == 0
    retired = classification["declared_retirements"]["items"]
    assert [entry["model_id"] for entry in retired] == ["basins_bravo_shud"]
    assert retired[0]["old_checksum"] == previous_rows["basins_bravo_shud"]["package_checksum"]
    assert retired[0]["new_checksum"] is None
    assert retired[0]["transition_mode"] == "retire"
    # The generation the operator copied off the refusal is the one the
    # accepting run bound to — that identity is what makes the runbook loop
    # terminate instead of chasing a moving value.
    assert classification["generation"] == refused_generation
