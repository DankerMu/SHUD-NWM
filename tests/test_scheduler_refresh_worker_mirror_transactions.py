"""The worker-registry mirror and the four-lane provider transaction.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Covers the dry-run that
validates three providers without replacing bytes, direct-grid republication of
the current authority, shared/private root routing, the worker-registry
generation binding and its dry-run mismatch refusal, the direct-grid mirror
dry-run family (entry counts, byte/preimage invariance, receipt persistence,
the two empty-model fail-closed cases), and the tracked four-lane rollback
family: readiness failure, state failure, CAS conflict, typed preimage
conflict and the generic write-after-exception uncertainty. The
restored-provider evidence of the ``replace_uncertain`` receipt moved to
``tests/test_scheduler_refresh_restored_receipt_truth.py`` (#2297).
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.object_store import sha256_bytes
from packages.common.provider_atomic import (
    ProviderPreimage,
    atomic_replace_provider_bytes,
)
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
)
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination
from tests.scheduler_refresh_helpers import (
    _config,
    _minimal_registry_manifest_bytes,
    _registry_row,
    _stub_provider_pipeline,
    _tracked_transaction_fixture,
    _valid_previous_manifest,
)


def _preimage(value: str = "old") -> ProviderPreimage:
    return ProviderPreimage(
        exists=True,
        sha256=value * (64 // len(value)) if len(value) < 64 else value[:64],
        device=1,
        inode=2,
        mode=0o600,
        uid=1,
        gid=1,
        size=10,
        mtime_ns=20,
    )


def _seed_empty_provider_files(config: refresh.RefreshConfig) -> None:
    generated = refresh.datetime.now(refresh.UTC)
    publish_scheduler_registry_manifest(
        [],
        config.registry_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )
    publish_canonical_readiness_index(
        [],
        config.readiness_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )
    publish_state_snapshot_index(
        [],
        config.state_uri,
        object_store_root=config.object_store_root,
        object_store_prefix=config.object_store_prefix,
        generated_at=generated,
    )


def _stub_catalog_bound_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        refresh,
        "derive_catalog_bound_readiness_entries",
        lambda *args, **kwargs: ([{"catalog_bound": True}], {"status": "ready", "entry_count": 1}),
    )
    monkeypatch.setattr(refresh, "validate_catalog_bound_readiness_entries", lambda *args, **kwargs: {})


def test_refresh_dry_run_validates_three_providers_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert receipt["database_free"] is True
    assert [provider["name"] for provider in receipt["providers"]] == ["registry", "readiness", "state"]
    assert receipt["providers"][0]["entry_count"] == 13
    assert receipt["providers"][1]["entry_count"] == 26
    assert all(provider["after_sha256"] == provider["before_sha256"] for provider in receipt["providers"])
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt
    assert not list(config.emergency_root.iterdir())


def test_direct_grid_refresh_republishes_current_authority_instead_of_basins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_DIRECT_GRID", "true")
    models = [
        {"model_id": f"dg-{index}", "basin_id": f"basin-{index}"}
        for index in range(36)
    ]
    previous = _minimal_registry_manifest_bytes("direct-grid-current")
    monkeypatch.setattr(
        refresh,
        "_load_previous_canonical_registry",
        lambda *args, **kwargs: (sha256_bytes(previous), models, previous),
    )
    monkeypatch.setattr(
        refresh,
        "publish_all_basin_scheduler_registry",
        lambda **kwargs: pytest.fail("direct-grid steady refresh must not republish Basins IDW rows"),
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert receipt["providers"][0]["entry_count"] == 36


def test_provider_evidence_prefers_entry_count_over_model_count() -> None:
    evidence = refresh._provider_evidence(
        "readiness",
        {},
        {"entry_count": 40, "model_count": 20},
    )

    assert evidence["entry_count"] == 40


def test_refresh_routes_shared_provider_and_private_reference_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _config(tmp_path)
    private_references = tmp_path / "private-reference-objects"
    private_references.mkdir()
    config = replace(original, object_store_root=private_references)
    _stub_provider_pipeline(monkeypatch)
    seen: dict[str, Path] = {}
    preimage = _preimage("a")

    def capture(*args: object, **kwargs: object) -> ProviderPreimage:
        del args
        seen["registry_capture"] = Path(str(kwargs["object_store_root"]))
        return preimage

    def readiness(*args: object, **kwargs: object):
        del args
        seen["readiness"] = Path(str(kwargs["object_store_root"]))
        return ([{"entry": "valid"}], {"status": "ready", "entry_count": 26})

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            seen["state"] = Path(str(kwargs["object_store_root"]))

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    def registry(**kwargs: object) -> dict[str, object]:
        seen["registry_publish"] = Path(str(kwargs["object_store_root"]))
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-1", "basin_id": "basin-1"}],
        )
        return {"selected_model_count": 13, "registry": None, "packages": []}

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(refresh, "derive_catalog_bound_readiness_entries", readiness)
    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", registry)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert seen["registry_capture"] == config.provider_store_root
    assert seen["registry_publish"] == config.object_store_root
    assert seen["readiness"] == config.object_store_root
    assert seen["state"] == config.object_store_root


def test_refresh_commits_identical_worker_registry_generation_and_binds_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert [provider["name"] for provider in receipt["providers"]] == [
        "registry",
        "registry_worker_mirror",
        "readiness",
        "state",
    ]
    assert receipt["providers"][0]["after_sha256"] == receipt["providers"][1]["after_sha256"]
    assert receipt["providers"][0]["entry_count"] == receipt["providers"][1]["entry_count"] == 13
    assert paths["registry"].read_bytes() == paths["registry_worker_mirror"].read_bytes()


def test_refresh_dry_run_rejects_existing_worker_registry_generation_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    shared = Path(config.registry_uri)
    worker = Path(config.worker_registry_uri)
    # Both files must be shape-valid so the #1080 gate does not refuse
    # earlier than the shared/mirror generation-mismatch check.
    write_provider_destination(shared, _minimal_registry_manifest_bytes("shared"))
    make_directory_with_explicit_mode(worker.parent)  # lock parent, #1513
    write_provider_destination(worker, _minimal_registry_manifest_bytes("worker-old"))
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture_scheduler_provider_preimage)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_invalid"


def _direct_grid_mirror_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: list[dict[str, object]],
) -> tuple[refresh.RefreshConfig, dict[str, Path]]:
    """#1926: the live node-22 shape — direct-grid authority plus a worker
    registry mirror that is a byte-copy of the canonical registry.

    Real bytes land on both destinations and the REAL preimage capture is
    restored over ``_stub_provider_pipeline``'s ``exists=False`` sentinel, so
    the dry-run canonical/mirror digest guard (`:743-744`) compares real
    digests instead of two ``None``s.
    """
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    content = _valid_previous_manifest(models)
    registry = Path(config.registry_uri)
    worker = Path(config.worker_registry_uri)
    write_provider_destination(registry, content)
    make_directory_with_explicit_mode(worker.parent)  # lock parent, #1513
    write_provider_destination(worker, content)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(
        refresh, "capture_scheduler_provider_preimage", capture_scheduler_provider_preimage
    )
    monkeypatch.setattr(
        refresh,
        "publish_all_basin_scheduler_registry",
        lambda **kwargs: pytest.fail("direct-grid dry-run must not republish Basins IDW rows"),
    )
    monkeypatch.delenv(refresh.CUTOVER_DECLARATION_ENV, raising=False)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_DIRECT_GRID", "true")
    return config, {"registry": registry, "registry_worker_mirror": worker}


def _provider_snapshot(paths: dict[str, Path]) -> dict[str, tuple[bytes, dict[str, Any]]]:
    return {
        name: (
            path.read_bytes(),
            capture_scheduler_provider_preimage(str(path)).to_dict(),
        )
        for name, path in paths.items()
    }


# R16 / R17: counts agree at the 1-model boundary and at a multi-model set.
# The expected value is derived from the fixture's own model list, never a
# literal count.
@pytest.mark.parametrize("model_count", [1, 7])
def test_dry_run_worker_mirror_entry_count_equals_prospective_registry_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_count: int
) -> None:
    """#1926: under direct-grid authority with a worker mirror configured, a
    dry-run must report the mirror with the SAME prospective model count as the
    canonical registry, so `_validate_receipt`'s registry/mirror `entry_count`
    equality holds and the run produces a self-valid `outcome=dry_run` receipt
    instead of dying as `primary_receipt_failed`.
    """
    models = [_registry_row(f"dg-{index}", f"{index:064d}") for index in range(model_count)]
    config, paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=models)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run", receipt
    assert receipt["reason"] == "dry_run_complete", receipt
    assert receipt["phase"] == "complete", receipt
    assert [provider["name"] for provider in receipt["providers"]] == [
        "registry",
        "registry_worker_mirror",
        "readiness",
        "state",
    ]
    registry_provider, mirror_provider = receipt["providers"][0], receipt["providers"][1]
    assert registry_provider["entry_count"] == len(models)
    assert mirror_provider["entry_count"] == registry_provider["entry_count"]
    # The dry-run mirror is still a pure before-image on every byte field.
    assert mirror_provider["after_sha256"] == mirror_provider["before_sha256"]
    assert registry_provider["after_sha256"] == mirror_provider["after_sha256"]


# R18: a dry-run mutates no provider byte and no provider stat tuple.
def test_dry_run_with_worker_mirror_leaves_every_provider_byte_and_preimage_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = [_registry_row(f"dg-{index}", f"{index:064d}") for index in range(4)]
    config, paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=models)
    readiness = Path(config.readiness_uri)
    state = Path(config.state_uri)
    before = _provider_snapshot(paths)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run", receipt
    assert _provider_snapshot(paths) == before
    # Readiness and state were absent going in; a dry-run must not create them.
    assert not readiness.exists()
    assert not state.exists()


# R19: the successful dry-run receipt is persisted on both channels and the
# run's emergency reservation is released rather than left as a 0-byte slot.
def test_dry_run_with_worker_mirror_persists_receipt_and_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = [_registry_row(f"dg-{index}", f"{index:064d}") for index in range(3)]
    config, _paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=models)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run", receipt
    latest = json.loads((config.receipt_root / "latest.json").read_text())
    assert latest == receipt
    history = config.receipt_root / "history" / f"{receipt['run_id']}.json"
    assert json.loads(history.read_text()) == receipt
    assert list(config.emergency_root.iterdir()) == []
    # The persisted receipt passes the same validator `--enable` runs.
    assert refresh._validate_receipt(latest) == latest


# R17b (direct-grid path, `:896-898`): an empty prospective model set fails
# closed BEFORE any terminal dry-run receipt exists.  A zero-count `dry_run`
# receipt is unreachable by design and MUST NOT be made reachable.
def test_dry_run_over_empty_direct_grid_model_set_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=[])
    # Anchor the assertion to `:896-898` specifically. `:961-962` (empty
    # readiness) produces a receipt of the IDENTICAL shape, so without this spy
    # deleting the direct-grid guard would still pass this test: the run would
    # simply fail one gate later, for a different reason, and nothing would
    # notice. `precommit_provider_generation` is the very next statement after
    # the guard and its FIRST act is `_registry_precommit_gate`, so "the gate
    # was never invoked" is exactly "the guard at `:897` raised".
    # (`precommit_provider_generation` itself is a closure local to
    # `refresh_scheduler_file_providers`, so it cannot be patched directly;
    # `_registry_precommit_gate` is the module-level name it reaches through.)
    precommit_calls: list[tuple[object, ...]] = []
    real_gate = refresh._registry_precommit_gate

    def spy(*args: object, **kwargs: object) -> object:
        precommit_calls.append(args)
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(refresh, "_registry_precommit_gate", spy)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert precommit_calls == [], (
        "the direct-grid empty-set guard did not raise: execution reached the "
        "precommit call below it, so this receipt proves a LATER gate, not `:897`"
    )
    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "provider_invalid", receipt
    assert receipt["providers"] == []
    latest = json.loads((config.receipt_root / "latest.json").read_text())
    assert latest["outcome"] == "failed"
    assert latest["reason"] == "provider_invalid"


# R17b (non-direct-grid path, `:961-962`): empty readiness entries fail closed
# on the same terms.
def test_dry_run_over_empty_readiness_entries_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = [_registry_row(f"dg-{index}", f"{index:064d}") for index in range(2)]
    config, _paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=models)
    monkeypatch.delenv("NHMS_SCHEDULER_REQUIRE_DIRECT_GRID", raising=False)
    registry_models = [
        {"model_id": str(row["model_id"]), "basin_id": str(row["basin_id"])} for row in models
    ]
    # `readiness_entries` is initialised to `[]` at `:787` and only ever
    # assigned inside `precommit_provider_generation`, so a publisher stub that
    # skips `precommit_validator` would trip `:961` merely because the
    # derivation never ran -- passing for the wrong reason and proving nothing
    # about an empty derivation result.  Drive the real callback with a
    # NON-empty prospective set, then let the derivation itself come back empty.
    derivation_calls: list[int] = []

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](workspace, [], registry_models)
        return {"selected_model_count": len(registry_models), "registry": None, "packages": []}

    def empty_readiness(*args: object, **kwargs: object):
        del args, kwargs
        derivation_calls.append(1)
        return [], {"status": "ready", "entry_count": 0}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(refresh, "derive_catalog_bound_readiness_entries", empty_readiness)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert derivation_calls == [1], "the derivation must actually run for this test to bite"
    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "provider_invalid", receipt
    assert receipt["providers"] == []
    latest = json.loads((config.receipt_root / "latest.json").read_text())
    assert latest["outcome"] == "failed"


# R21: a dry-run that fails after the precommit gate keeps its own reason and
# is explicitly not folded to `primary_receipt_failed`.
def test_dry_run_failure_after_the_gate_is_not_folded_to_primary_receipt_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    models = [_registry_row(f"dg-{index}", f"{index:064d}") for index in range(2)]
    config, _paths = _direct_grid_mirror_fixture(tmp_path, monkeypatch, models=models)

    def failing_readiness_derivation(*args: object, **kwargs: object):
        del args, kwargs
        raise refresh.RefreshError("provider_preimage_changed")

    monkeypatch.setattr(
        refresh, "derive_catalog_bound_readiness_entries", failing_readiness_derivation
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "failed", receipt
    assert receipt["reason"] == "provider_preimage_changed", receipt
    assert receipt["reason"] != "primary_receipt_failed"
    latest = json.loads((config.receipt_root / "latest.json").read_text())
    assert latest["reason"] == "provider_preimage_changed"


def test_worker_registry_restore_uses_committed_preimage_and_restores_exact_bytes(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/registry/manifest-last.json"),
    )
    worker = Path(config.worker_registry_uri)
    make_directory_with_explicit_mode(worker.parent)  # lock parent, #1513
    write_provider_destination(worker, b"old-generation")
    before = capture_scheduler_provider_preimage(worker)
    committed = atomic_replace_provider_bytes(
        worker,
        b"prospective-generation",
        containment_root=config.object_store_root,
        max_bytes=refresh.MAX_REGISTRY_MANIFEST_BYTES,
        expected_preimage=before,
    )

    refresh._restore_worker_registry_mirror(config, previous=b"old-generation", expected_current=committed)

    assert worker.read_bytes() == b"old-generation"


def test_refresh_rolls_back_worker_mirror_when_shared_registry_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="registry")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert {name: path.read_bytes() for name, path in paths.items()} == old


def test_readiness_failure_rolls_back_registry_mirror_and_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="readiness")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert receipt["providers"] == []
    assert {name: path.read_bytes() for name, path in paths.items()} == old
    assert not list(config.emergency_root.iterdir())


def test_state_failure_rolls_back_all_four_provider_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="state")
    original_restore = refresh._restore_provider_path
    rollback_order: list[Path] = []

    def record_restore(path: Path, **kwargs: object) -> None:
        rollback_order.append(path)
        original_restore(path, **kwargs)

    monkeypatch.setattr(refresh, "_restore_provider_path", record_restore)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous"
    assert receipt["providers"] == []
    assert {name: path.read_bytes() for name, path in paths.items()} == old
    assert rollback_order == [
        paths["state"],
        paths["readiness"],
        paths["registry"],
        paths["registry_worker_mirror"],
    ]


def test_rollback_cas_conflict_is_uncertain_and_never_relabelled_as_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, old, paths = _tracked_transaction_fixture(tmp_path, monkeypatch, fail_lane="state")
    original_restore = refresh._restore_provider_path

    def conflict_readiness(path: Path, **kwargs: object) -> None:
        if path == paths["readiness"]:
            path.write_bytes(b"concurrent-authoritative-readiness")
        original_restore(path, **kwargs)

    monkeypatch.setattr(refresh, "_restore_provider_path", conflict_readiness)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    assert receipt["reason"] == "provider_replace_uncertain"
    assert paths["registry"].read_bytes() == old["registry"]
    assert paths["registry_worker_mirror"].read_bytes() == old["registry_worker_mirror"]
    assert paths["state"].read_bytes() == old["state"]
    assert paths["readiness"].read_bytes() == b"concurrent-authoritative-readiness"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert json.loads(emergency[0].read_text())["outcome"] == "replace_uncertain"


@pytest.mark.parametrize(
    "lane",
    ["registry_worker_mirror", "registry", "readiness", "state"],
)
def test_typed_preimage_conflict_preserves_authoritative_lane_and_restores_earlier_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    config, old, paths = _tracked_transaction_fixture(
        tmp_path,
        monkeypatch,
        fail_lane="",
        conflict_lane=lane,
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_preimage_changed"
    assert receipt["providers"] == []
    assert paths[lane].read_bytes() == f"concurrent-authoritative-{lane}".encode()
    assert {name: path.read_bytes() for name, path in paths.items() if name != lane} == {
        name: content for name, content in old.items() if name != lane
    }


def test_generic_write_after_exception_without_commit_token_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, old, paths = _tracked_transaction_fixture(
        tmp_path,
        monkeypatch,
        fail_lane="",
        unowned_lane="state",
    )

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "replace_uncertain"
    assert receipt["reason"] == "provider_replace_uncertain"
    assert paths["state"].read_bytes() == b"new-state-generation"
    assert paths["registry"].read_bytes() == old["registry"]
    assert paths["registry_worker_mirror"].read_bytes() == old["registry_worker_mirror"]
    assert paths["readiness"].read_bytes() == old["readiness"]


@pytest.mark.parametrize("lane", ["readiness", "state"])
def test_full_refresh_preserves_newer_authoritative_provider_on_snapshot_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    config = _config(tmp_path)
    _seed_empty_provider_files(config)
    _stub_catalog_bound_derivation(monkeypatch)
    authoritative: dict[str, bytes] = {}

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda _entries, destination, **kwargs: publish_canonical_readiness_index([], destination, **kwargs),
    )
    if lane == "readiness":
        def derive_then_replace(*args: object, **kwargs: object):
            del args, kwargs
            publish_canonical_readiness_index(
                [],
                config.readiness_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                generated_at=refresh.datetime.now(refresh.UTC) + timedelta(seconds=1),
            )
            authoritative["bytes"] = Path(config.readiness_uri).read_bytes()
            return ([{"catalog_bound": True}], {"status": "ready", "entry_count": 1})

        monkeypatch.setattr(refresh, "derive_catalog_bound_readiness_entries", derive_then_replace)
        destination = Path(config.readiness_uri)
    else:
        repository_type = refresh.FileStateSnapshotIndexRepository

        class ReplaceAfterStateSnapshot(repository_type):
            def validated_entries_for_renewal(self):
                snapshot = super().validated_entries_for_renewal()
                publish_state_snapshot_index(
                    [],
                    config.state_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    generated_at=refresh.datetime.now(refresh.UTC) + timedelta(seconds=1),
                )
                authoritative["bytes"] = Path(config.state_uri).read_bytes()
                return snapshot

        monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", ReplaceAfterStateSnapshot)
        destination = Path(config.state_uri)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)
    assert receipt["outcome"] == "failed"
    assert receipt["reason"] == "provider_preimage_changed"
    assert destination.read_bytes() == authoritative["bytes"]


def test_full_refresh_maps_public_postread_failure_to_restored_previous_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _seed_empty_provider_files(config)
    _stub_catalog_bound_derivation(monkeypatch)
    readiness_before = Path(config.readiness_uri).read_bytes()

    def publish_registry(**kwargs: object) -> dict[str, object]:
        workspace = Path(str(kwargs["work_dir"]))
        workspace.mkdir(parents=True, exist_ok=True)
        kwargs["precommit_validator"](
            workspace,
            [],
            [{"model_id": "model-a", "basin_id": "basin-a"}],
        )
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda _entries, destination, **kwargs: publish_canonical_readiness_index([], destination, **kwargs),
    )
    real_capture = provider_atomic_module.capture_provider_preimage
    readiness_capture_count = 0

    def fail_readiness_postread(path: Path, *args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal readiness_capture_count
        if Path(path) == Path(config.readiness_uri):
            readiness_capture_count += 1
            if readiness_capture_count == 2:
                raise SafeFilesystemError("injected readiness post-read failure")
        return real_capture(path, *args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", fail_readiness_postread)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "restored_previous", receipt
    assert receipt["reason"] == "provider_postread_failed"
    assert receipt["phase"] == "postcommit"
    assert Path(config.readiness_uri).read_bytes() == readiness_before


def test_refresh_publishes_three_provider_digests_and_keeps_32_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    history = config.receipt_root / "history"
    history.mkdir()
    for index in range(35):
        (history / f"old-{index:02d}.json").write_text("{}")

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert [item["after_sha256"] for item in receipt["providers"]] == ["d" * 64, "e" * 64, "f" * 64]
    assert len(list(history.iterdir())) == 32
