"""Emergency reconstruction, receipt bounds and the workspace budget.

Partition (#1101 partition of the 9614-line / 315-case
tests/test_scheduler_file_provider_refresh.py). Covers the snapshot-race and
public post-read failure mappings, the three-digest publish with 32-deep
history, both emergency finalisation failure modes, emergency reconstruction
validation, current-receipt validation, the receipt bounds corpus and the
schema/runtime negative-corpus agreement, receipt monotonicity -- then the
emergency slot's short-write and fsync failure legs, the durable reservation,
and the workspace depth/bytes/entry/inventory budget refusals.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
)
from tests.scheduler_refresh_helpers import (
    _config,
    _stub_provider_pipeline,
    _write_current_published_receipt,
)
from tests.scheduler_refresh_receipt_helpers import (
    _classification_stub,
    _enforced_cutover_gate,
)


def test_primary_receipt_failure_after_commit_finalizes_mode_0600_emergency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published_receipt_failed"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert os.stat(emergency[0]).st_mode & 0o777 == 0o600
    emergency_receipt = json.loads(emergency[0].read_text())
    assert refresh._validate_receipt(emergency_receipt) == emergency_receipt
    assert emergency_receipt["providers"][2]["after_sha256"] == "f" * 64


def test_primary_and_emergency_receipt_failure_is_replace_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    def fail_emergency(slot: refresh.EmergencySlot, receipt: object) -> None:
        del receipt
        os.close(slot.file_fd)
        os.close(slot.parent_fd)
        raise OSError

    monkeypatch.setattr(refresh, "_finalize_emergency_slot", fail_emergency)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"


def test_emergency_reconstruction_validates_committed_digests_without_republish(tmp_path: Path) -> None:
    config = _config(tmp_path)
    providers = []
    for name, uri in (
        ("registry", config.registry_uri),
        ("readiness", config.readiness_uri),
        ("state", config.state_uri),
    ):
        path = Path(uri)
        path.write_text(name)
        preimage = capture_scheduler_provider_preimage(uri)
        providers.append(
            {
                "name": name,
                "before_sha256": None,
                "before_inode": None,
                "before_schema_version": None,
                "before_generated_at": None,
                "before_payload_checksum": None,
                "after_sha256": preimage.sha256,
                "after_schema_version": None,
                "after_generated_at": None,
                "after_payload_checksum": None,
                "entry_count": 1,
            }
        )
    receipt = refresh._receipt(
        run_id="refresh_reconstruct",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=providers,
    )
    emergency = config.emergency_root / "refresh_reconstruct.reserved.json"
    emergency.write_bytes(refresh._receipt_bytes(receipt))
    emergency.chmod(0o600)

    reconstructed = refresh.reconstruct_primary_receipt(config, emergency)

    assert reconstructed == receipt
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt


def test_emergency_reconstruction_rejects_noncanonical_receipt_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    receipt = refresh._receipt(
        run_id="refresh_invalid_recovery",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=[],
    )
    emergency = config.emergency_root / "refresh_invalid_recovery.reserved.json"
    emergency.write_text(json.dumps({**receipt, "unexpected": "smuggled"}))
    published = False

    def record_publish(*args: object, **kwargs: object) -> None:
        nonlocal published
        del args, kwargs
        published = True

    monkeypatch.setattr(refresh, "_publish_primary_receipt", record_publish)
    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.reconstruct_primary_receipt(config, emergency)

    assert error_info.value.reason == "emergency_record_invalid"
    assert not published


def test_current_receipt_validation_rejects_untrusted_or_stale_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    receipt_path, receipt = _write_current_published_receipt(config)
    assert refresh.validate_current_receipt(config, receipt_path) == receipt

    receipt_path.write_text('{"outcome":"published","database_free":true}\n')
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.write_bytes(refresh._receipt_bytes({**receipt, "unexpected": True}))
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.write_bytes(b"{" + b'"padding":"' + b"x" * refresh.MAX_RECEIPT_BYTES + b'"}')
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.unlink()
    target = config.receipt_root / "target.json"
    target.write_bytes(refresh._receipt_bytes(receipt))
    receipt_path.symlink_to(target)
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    receipt_path.unlink()
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    Path(config.registry_uri).write_text("changed\n")
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    Path(config.registry_uri).unlink()
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)
    non_published = {
        **receipt,
        "outcome": "failed",
        "operation_outcome": "failed",
        "reason": "provider_invalid",
        "operation_reason": "provider_invalid",
    }
    receipt_path.write_bytes(refresh._receipt_bytes(non_published))
    with pytest.raises(refresh.RefreshError):
        refresh.validate_current_receipt(config, receipt_path)


def test_current_receipt_validation_rejects_worker_registry_generation_mismatch(tmp_path: Path) -> None:
    config = replace(
        _config(tmp_path),
        worker_registry_uri=str(tmp_path / "objects/scheduler/worker-registry/manifest-last.json"),
    )
    receipt_path, receipt = _write_current_published_receipt(config)
    shared = Path(config.registry_uri)
    worker = Path(config.worker_registry_uri)
    worker.write_bytes(shared.read_bytes())
    shared_preimage = capture_scheduler_provider_preimage(shared)
    worker_preimage = capture_scheduler_provider_preimage(worker)
    for provider in receipt["providers"]:
        if provider["name"] == "registry":
            provider["after_sha256"] = shared_preimage.sha256
        elif provider["name"] == "registry_worker_mirror":
            provider["after_sha256"] = worker_preimage.sha256
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    assert refresh.validate_current_receipt(config, receipt_path) == receipt

    worker.write_text("stale-worker-generation\n")
    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.validate_current_receipt(config, receipt_path)

    assert error_info.value.reason == "emergency_record_invalid"


def test_receipt_bounds_reject_long_strings_and_unknown_outcomes() -> None:
    receipt = refresh._receipt(
        run_id="run",
        started=refresh.datetime.now(refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="complete",
        providers=[],
    )
    refresh._validate_receipt(receipt)
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "outcome": "partial"})
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "phase": "x" * 513})
    boundary = {
        "items": ["package:" + "a" * 32],
        "total": refresh.MAX_ORPHANS,
        "discovered_total": refresh.MAX_ORPHANS,
        "attempted_total": refresh.MAX_ORPHANS,
        "created_total": refresh.MAX_ORPHANS,
        "truncated": True,
    }
    refresh._validate_receipt({**receipt, "orphans": boundary})
    with pytest.raises(ValueError):
        refresh._validate_receipt(
            {
                **receipt,
                "orphans": {
                    **boundary,
                    "total": refresh.MAX_ORPHANS + 1,
                    "discovered_total": refresh.MAX_ORPHANS + 1,
                    "attempted_total": refresh.MAX_ORPHANS + 1,
                    "created_total": refresh.MAX_ORPHANS + 1,
                },
            }
        )
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "residues": ["../outside"]})
    with pytest.raises(ValueError):
        refresh._receipt_bytes({"padding": "x" * refresh.MAX_RECEIPT_BYTES})
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "unexpected": True})
    with pytest.raises(ValueError):
        refresh._validate_receipt(
            {**receipt, "orphans": {**receipt["orphans"], "unexpected": True}}
        )


def test_receipt_schema_and_runtime_reject_same_expressible_negative_corpus() -> None:
    provider = {
        "name": "registry",
        "before_sha256": "a" * 64,
        "before_inode": 1,
        "before_schema_version": "v1",
        "before_generated_at": "2026-07-14T00:00:00Z",
        "before_payload_checksum": "sha256:" + "b" * 64,
        "after_sha256": "c" * 64,
        "after_schema_version": "v1",
        "after_generated_at": "2026-07-14T01:00:00Z",
        "after_payload_checksum": "sha256:" + "d" * 64,
        "entry_count": 1,
    }
    receipt = refresh._receipt(
        run_id="refresh_valid",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=[
            provider,
            {**provider, "name": "registry_worker_mirror"},
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
        registry_classification=_classification_stub(),
        # #1144: `published` receipts must carry the audit block on both sides.
        cutover_gate=_enforced_cutover_gate(declaration_present=False),
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/scheduler_file_provider_refresh_receipt.schema.json"
        ).read_text()
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    refresh._validate_receipt(receipt)
    validator.validate(receipt)
    boundary = {
        **receipt,
        "orphans": {
            "items": [f"package:{index:032x}" for index in range(256)],
            "total": 4096,
            "discovered_total": 4096,
            "attempted_total": 4096,
            "created_total": 4096,
            "truncated": True,
        },
    }
    refresh._validate_receipt(boundary)
    validator.validate(boundary)
    overflow = {
        **boundary,
        "orphans": {
            **boundary["orphans"],
            "total": 4097,
            "discovered_total": 4097,
            "attempted_total": 4097,
            "created_total": 4097,
        },
    }
    with pytest.raises(ValueError):
        refresh._validate_receipt(overflow)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(overflow)
    invalid_receipts = [
        {**receipt, "run_id": "/"},
        {
            **receipt,
            "providers": [{**receipt["providers"][0], "after_sha256": "a"}, *receipt["providers"][1:]],
        },
        {**receipt, "providers": list(reversed(receipt["providers"]))},
    ]
    for invalid in invalid_receipts:
        with pytest.raises(ValueError):
            refresh._validate_receipt(invalid)
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(invalid)


def test_receipt_latest_is_monotonic_and_history_keeps_both(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    newer = refresh._receipt(
        run_id="refresh_newer",
        started=refresh.datetime(2026, 7, 14, 7, tzinfo=refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="precommit",
        providers=[],
    )
    older = refresh._receipt(
        run_id="refresh_older",
        started=refresh.datetime(2026, 7, 14, 6, tzinfo=refresh.UTC),
        outcome="failed",
        reason="provider_invalid",
        phase="precommit",
        providers=[],
    )

    refresh._publish_primary_receipt(root, newer)
    refresh._publish_primary_receipt(root, older)

    assert json.loads((root / "latest.json").read_text()) == newer
    assert {path.stem for path in (root / "history").iterdir()} == {"refresh_newer", "refresh_older"}


def test_emergency_slot_handles_short_writes_and_validates_complete_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "emergency"
    root.mkdir(mode=0o700)
    receipt = refresh._receipt(
        run_id="refresh_short_writes",
        started=refresh.datetime.now(refresh.UTC),
        outcome="published_receipt_failed",
        reason="primary_receipt_failed",
        phase="receipt",
        providers=[],
    )
    slot = refresh._reserve_emergency_slot(root, str(receipt["run_id"]))
    real_write = os.write

    def short_write(fd: int, content: object) -> int:
        return real_write(fd, memoryview(content)[:1])

    monkeypatch.setattr(refresh.os, "write", short_write)
    refresh._finalize_emergency_slot(slot, receipt)

    recovered = json.loads((root / "refresh_short_writes.reserved.json").read_text())
    assert refresh._validate_receipt(recovered) == receipt


def test_full_refresh_short_writes_still_publish_complete_emergency_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    real_write = os.write

    def short_write(fd: int, content: object) -> int:
        return real_write(fd, memoryview(content)[:3])

    monkeypatch.setattr(refresh.os, "write", short_write)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published_receipt_failed"
    emergency = list(config.emergency_root.iterdir())
    assert len(emergency) == 1
    assert refresh._validate_receipt(json.loads(emergency[0].read_text()))["run_id"] == receipt["run_id"]


def test_full_refresh_zero_progress_emergency_write_is_uncertain_and_leak_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    captured: list[refresh.EmergencySlot] = []
    real_reserve = refresh._reserve_emergency_slot

    def capture_reserve(root: Path, run_id: str) -> refresh.EmergencySlot:
        slot = real_reserve(root, run_id)
        captured.append(slot)
        return slot

    monkeypatch.setattr(refresh, "_reserve_emergency_slot", capture_reserve)
    monkeypatch.setattr(refresh.os, "write", lambda fd, content: 0)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"
    assert list(config.emergency_root.iterdir()) == []
    assert captured
    for fd in (captured[0].file_fd, captured[0].parent_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure_target", ["file", "parent"])
def test_full_refresh_finalize_fsync_failure_is_uncertain_and_leak_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))
    captured: list[refresh.EmergencySlot] = []
    real_reserve = refresh._reserve_emergency_slot
    real_fsync = os.fsync

    def capture_reserve(root: Path, run_id: str) -> refresh.EmergencySlot:
        slot = real_reserve(root, run_id)
        captured.append(slot)
        return slot

    def fail_finalize_fsync(fd: int) -> None:
        if captured:
            target_fd = captured[0].file_fd if failure_target == "file" else captured[0].parent_fd
            if fd == target_fd:
                raise OSError(f"injected {failure_target} fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(refresh, "_reserve_emergency_slot", capture_reserve)
    monkeypatch.setattr(refresh.os, "fsync", fail_finalize_fsync)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.outcome == "replace_uncertain"
    assert error_info.value.reason == "receipt_channels_failed"
    assert list(config.emergency_root.iterdir()) == []
    for fd in (captured[0].file_fd, captured[0].parent_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure_call", [1, 2])
def test_full_refresh_reserve_fsync_failure_cleans_workspace_slot_and_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    config = _config(tmp_path)
    opened: dict[str, int] = {}
    real_open_directory = refresh.open_directory_no_follow
    real_open = os.open
    real_fsync = os.fsync
    fsync_calls = 0

    def capture_parent(*args: object, **kwargs: object) -> int:
        fd = real_open_directory(*args, **kwargs)
        opened["parent"] = fd
        return fd

    def capture_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            opened["file"] = fd
        return fd

    def fail_reserve_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == failure_call:
            raise OSError("injected reserve fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(refresh, "open_directory_no_follow", capture_parent)
    monkeypatch.setattr(refresh.os, "open", capture_file)
    monkeypatch.setattr(refresh.os, "fsync", fail_reserve_fsync)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert error_info.value.reason == "primary_receipt_failed"
    assert list(config.emergency_root.iterdir()) == []
    assert list(config.workspace_root.iterdir()) == []
    assert set(opened) == {"parent", "file"}
    for fd in opened.values():
        with pytest.raises(OSError):
            os.fstat(fd)


def test_emergency_reservation_is_durable_before_first_provider_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    events: list[str] = []
    opened: dict[str, int] = {}
    real_open_directory = refresh.open_directory_no_follow
    real_open = os.open
    real_fsync = os.fsync
    registry_publisher = refresh.publish_all_basin_scheduler_registry

    def capture_parent(*args: object, **kwargs: object) -> int:
        fd = real_open_directory(*args, **kwargs)
        opened["parent"] = fd
        return fd

    def capture_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            opened["file"] = fd
        return fd

    def record_fsync(fd: int) -> None:
        if fd == opened.get("file"):
            events.append("reserve_file_fsync")
        elif fd == opened.get("parent"):
            events.append("reserve_parent_fsync")
        real_fsync(fd)

    def record_provider(**kwargs: object) -> dict[str, object]:
        events.append("provider_side_effect")
        return registry_publisher(**kwargs)

    monkeypatch.setattr(refresh, "open_directory_no_follow", capture_parent)
    monkeypatch.setattr(refresh.os, "open", capture_file)
    monkeypatch.setattr(refresh.os, "fsync", record_fsync)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", record_provider)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=False)

    assert receipt["outcome"] == "published"
    assert events[:3] == ["reserve_file_fsync", "reserve_parent_fsync", "provider_side_effect"]


def test_workspace_bounds_reject_depth_entry_bytes_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "data").write_bytes(b"12345")
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_DEPTH", 1)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_DEPTH", 32)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_BYTES", 4)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)
    monkeypatch.setattr(refresh, "MAX_WORKSPACE_BYTES", 100)
    (root / "unsafe").symlink_to(nested)
    with pytest.raises(refresh.RefreshError):
        refresh._enforce_workspace_bounds(root)


def test_workspace_budget_rejects_oversized_copy_before_file_creation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "too-large.bin").write_bytes(b"12345")
    budget = refresh._WorkspaceBudget(root, max_bytes=4, max_entries=10, max_depth=10)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert not (root / "copy" / "too-large.bin").exists()


def test_workspace_budget_rejects_entry_before_creating_over_limit_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "second-entry.txt").write_text("content", encoding="utf-8")
    budget = refresh._WorkspaceBudget(root, max_bytes=100, max_entries=1, max_depth=10)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert (root / "copy").is_dir()
    assert not (root / "copy" / "second-entry.txt").exists()


def test_workspace_budget_rejects_depth_before_creating_over_limit_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    budget = refresh._WorkspaceBudget(root, max_bytes=100, max_entries=10, max_depth=1)

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.copy_tree(source, root / "copy")

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert (root / "copy").is_dir()
    assert not (root / "copy" / "nested").exists()


def test_workspace_budget_rejects_inventory_write_before_file_creation(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    budget = refresh._WorkspaceBudget(root, max_bytes=1, max_entries=10, max_depth=10)
    inventory = root / "registry" / "basins-inventory.json"

    with pytest.raises(refresh.RefreshError) as error_info:
        budget.write_json(inventory, {"models": []})

    assert error_info.value.reason == "workspace_limit_exceeded"
    assert not inventory.exists()
