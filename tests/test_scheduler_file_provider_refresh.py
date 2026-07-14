from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    provider_destination_lock,
    read_provider_snapshot,
)
from packages.common.safe_fs import SafeFilesystemError
from packages.common.state_manager import publish_state_snapshot_index
from scripts import scheduler_file_provider_refresh as refresh
from services.orchestrator.scheduler_file_providers import (
    capture_scheduler_provider_preimage,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
)


def _config(tmp_path: Path) -> refresh.RefreshConfig:
    basins = tmp_path / "Basins"
    objects = tmp_path / "objects"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, objects, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (work, receipts, emergency, tmp_path / "private"):
        path.chmod(0o700)
    scheduler = objects / "scheduler"
    (scheduler / "registry").mkdir(parents=True)
    (scheduler / "canonical-readiness").mkdir()
    (scheduler / "state-index").mkdir()
    return refresh.RefreshConfig(
        basins_root=basins,
        registry_uri=str(scheduler / "registry" / "manifest-last.json"),
        readiness_uri=str(scheduler / "canonical-readiness" / "index-last.json"),
        state_uri=str(scheduler / "state-index" / "index-last.json"),
        object_store_root=objects,
        provider_store_root=objects,
        object_store_prefix="s3://nhms",
        workspace_root=work,
        receipt_root=receipts,
        emergency_root=emergency,
        refresh_lock=tmp_path / "private" / "refresh",
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


def _write_current_published_receipt(config: refresh.RefreshConfig) -> tuple[Path, dict[str, object]]:
    providers = []
    for name, uri in (
        ("registry", config.registry_uri),
        ("readiness", config.readiness_uri),
        ("state", config.state_uri),
    ):
        path = Path(uri)
        path.write_text(name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = refresh._receipt(
        run_id="refresh_current",
        started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
        outcome="published",
        reason="success",
        phase="complete",
        providers=providers,
    )
    receipt_path = config.receipt_root / "latest.json"
    receipt_path.write_bytes(refresh._receipt_bytes(receipt))
    return receipt_path, receipt


def _stub_provider_pipeline(monkeypatch: pytest.MonkeyPatch, *, committed: bool = True) -> None:
    del committed
    preimage = _preimage("a")
    registry_calls = 0

    def capture(uri: str, *args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal registry_calls
        del args, kwargs
        if "registry" in str(uri):
            registry_calls += 1
            return preimage if registry_calls == 1 else _preimage("d")
        if "canonical-readiness" in str(uri):
            return _preimage("e")
        return _preimage("f")

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(
        refresh,
        "_read_provider_header",
        lambda *args, **kwargs: {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-01T00:00:00Z",
            "checksum": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        refresh,
        "load_canonical_readiness_entries_for_renewal",
        lambda *args, **kwargs: ([{"entry": "valid"}], {"checksum": "sha256:" + "b" * 64}, preimage),
    )

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(
        refresh,
        "publish_all_basin_scheduler_registry",
        lambda **kwargs: {
            "selected_model_count": 13,
            "registry": None
            if kwargs["dry_run"]
            else {"checksum": "sha256:" + "d" * 64, "model_count": 13},
        },
    )
    monkeypatch.setattr(
        refresh,
        "publish_canonical_readiness_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "e" * 64, "entry_count": 1},
    )
    monkeypatch.setattr(
        refresh,
        "publish_state_snapshot_index",
        lambda *args, **kwargs: {"checksum": "sha256:" + "f" * 64, "entry_count": 1},
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


def test_provider_atomic_expected_preimage_preserves_concurrent_update(tmp_path: Path) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")
    expected = capture_provider_preimage(destination, max_bytes=1024)
    destination.write_bytes(b"authoritative-new")
    before = os.stat(destination)

    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(
            destination,
            b"refresh",
            max_bytes=1024,
            expected_preimage=expected,
        )

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"authoritative-new"
    after = os.stat(destination)
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_size,
        after.st_mtime_ns,
    )
    assert after_identity == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
    )


def test_provider_snapshot_rejects_replacement_between_metadata_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"generation-a")
    real_read = provider_atomic_module.read_bytes_limited_no_follow
    replaced = False

    def replace_before_read(*args: object, **kwargs: object) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            destination.write_bytes(b"generation-b")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "read_bytes_limited_no_follow", replace_before_read)
    with pytest.raises(ProviderAtomicError) as error_info:
        read_provider_snapshot(destination, max_bytes=1024)

    assert error_info.value.reason == "provider_preimage_changed"
    assert destination.read_bytes() == b"generation-b"


def test_provider_atomic_postread_failure_restores_validated_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")
    expected = capture_provider_preimage(destination, max_bytes=1024)
    real_write = provider_atomic_module.atomic_write_bytes_no_follow
    calls = 0

    def corrupt_first(path: Path, content: bytes, **kwargs: object) -> Path:
        nonlocal calls
        calls += 1
        return real_write(path, b"corrupt" if calls == 1 else content, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", corrupt_first)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024, expected_preimage=expected)

    assert error_info.value.reason == "provider_restored_previous"
    assert destination.read_bytes() == b"old"


def test_provider_atomic_durable_replace_uncertainty_is_not_reported_as_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "index-last.json"
    destination.write_bytes(b"old")

    def uncertain(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise SafeFilesystemError("directory fsync failed", kind="indeterminate")

    monkeypatch.setattr(provider_atomic_module, "atomic_write_bytes_no_follow", uncertain)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.reason == "provider_replace_uncertain"
    assert error_info.value.phase == "replace_uncertain"


def test_provider_destination_lock_contender_is_nonblocking(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    with provider_destination_lock(destination, blocking=False):
        with pytest.raises(ProviderAtomicError) as error_info:
            with provider_destination_lock(destination, blocking=False):
                pass
    assert error_info.value.reason == "provider_already_running"


def test_provider_destination_lock_still_excludes_cross_process_contender(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    repository = Path(__file__).resolve().parents[1]
    contender = """
import sys
from pathlib import Path
from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock

try:
    with provider_destination_lock(Path(sys.argv[1]), blocking=False):
        print("unexpectedly-acquired")
except ProviderAtomicError as error:
    print(error.reason)
"""

    with provider_destination_lock(destination):
        result = subprocess.run(
            [sys.executable, "-c", contender, str(destination)],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    assert result.stdout.strip() == "provider_already_running"


def test_provider_destination_lock_serializes_threads_when_flock_is_process_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", lambda *_args: None)
    start = threading.Barrier(20)
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    maximum_active = 0
    errors: list[BaseException] = []

    def contender() -> None:
        nonlocal active, maximum_active
        try:
            start.wait()
            with provider_destination_lock(destination):
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                    if active > 1:
                        release_first.set()
                release_first.wait(timeout=0.05)
                with guard:
                    active -= 1
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=contender) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert maximum_active == 1
    assert not provider_atomic_module._PROCESS_LOCKS


def test_provider_atomic_readers_observe_only_complete_old_or_new_json(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    old = json.dumps({"generation": "old", "rows": list(range(50))}).encode()
    new = json.dumps({"generation": "new", "rows": list(range(100))}).encode()
    destination.write_bytes(old)
    finished = threading.Event()
    observed: list[bytes] = []

    def writer() -> None:
        for index in range(40):
            atomic_replace_provider_bytes(destination, new if index % 2 else old, max_bytes=4096)
        finished.set()

    thread = threading.Thread(target=writer)
    thread.start()
    while not finished.is_set():
        observed.append(destination.read_bytes())
    thread.join()

    assert observed
    assert set(observed) <= {old, new}
    assert all(json.loads(content)["generation"] in {"old", "new"} for content in observed)


def test_provider_atomic_publishes_shared_mode_under_private_umask(tmp_path: Path) -> None:
    destination = tmp_path / "manifest-last.json"
    previous_umask = os.umask(0o077)
    try:
        atomic_replace_provider_bytes(destination, b"shared", max_bytes=1024)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert destination.stat().st_uid == os.geteuid()


def test_provider_lock_rejects_writable_parent_and_preserves_body_errors(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(unsafe / "manifest.json"):
            pass
    assert error_info.value.reason == "provider_lock_parent_unsafe"

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with provider_destination_lock(shared / "manifest.json"):
        pass

    with pytest.raises(SafeFilesystemError):
        with provider_destination_lock(tmp_path / "manifest.json"):
            raise SafeFilesystemError("body failure")


def test_provider_lock_revalidates_lock_path_after_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest.json"
    lock_path = provider_atomic_module.provider_lock_path(destination)
    real_flock = provider_atomic_module.fcntl.flock
    swapped = False

    def swap_after_lock(fd: int, flags: int) -> None:
        nonlocal swapped
        real_flock(fd, flags)
        if flags & provider_atomic_module.fcntl.LOCK_EX and not swapped:
            swapped = True
            replacement = tmp_path / "replacement.lock"
            replacement.write_bytes(b"")
            os.replace(replacement, lock_path)

    monkeypatch.setattr(provider_atomic_module.fcntl, "flock", swap_after_lock)
    with pytest.raises(ProviderAtomicError) as error_info:
        with provider_destination_lock(destination):
            pass

    assert error_info.value.reason == "provider_lock_changed"


def test_provider_postreplace_read_exception_is_not_precommit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "manifest-last.json"
    destination.write_bytes(b"old")
    real_capture = provider_atomic_module.capture_provider_preimage
    calls = 0

    def fail_after_replace(*args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise SafeFilesystemError("post-replace read failed")
        return real_capture(*args, **kwargs)

    monkeypatch.setattr(provider_atomic_module, "capture_provider_preimage", fail_after_replace)
    with pytest.raises(ProviderAtomicError) as error_info:
        atomic_replace_provider_bytes(destination, b"new", max_bytes=1024)

    assert error_info.value.phase == "replace_uncertain"
    assert error_info.value.reason == "provider_postread_failed"


@pytest.mark.parametrize("provider", ["registry", "readiness", "state"])
def test_all_provider_publishers_reject_changed_expected_preimage(tmp_path: Path, provider: str) -> None:
    destination = tmp_path / f"{provider}.json"
    generated = refresh.datetime(2026, 7, 14, 12, tzinfo=refresh.UTC)
    if provider == "registry":
        def publisher(expected=None):
            return publish_scheduler_registry_manifest(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    elif provider == "readiness":
        def publisher(expected=None):
            return publish_canonical_readiness_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    else:
        def publisher(expected=None):
            return publish_state_snapshot_index(
                [], destination, generated_at=generated, expected_preimage=expected
            )
    previous_umask = os.umask(0o077)
    try:
        publisher()
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    expected = capture_scheduler_provider_preimage(destination)
    authoritative = json.loads(destination.read_text())
    authoritative["extra_authoritative_field"] = "new"
    destination.write_text(json.dumps(authoritative))
    preserved = destination.read_bytes()

    with pytest.raises(Exception) as error_info:
        publisher(expected)

    assert getattr(error_info.value, "reason", "") == "provider_preimage_changed"
    assert destination.read_bytes() == preserved


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
    assert all(provider["after_sha256"] == provider["before_sha256"] for provider in receipt["providers"])
    assert json.loads((config.receipt_root / "latest.json").read_text()) == receipt
    assert not list(config.emergency_root.iterdir())


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
        return ([{"entry": "valid"}], {"checksum": "sha256:" + "b" * 64}, preimage)

    class Repository:
        def __init__(self, **kwargs: object) -> None:
            seen["state"] = Path(str(kwargs["object_store_root"]))

        def validated_entries_for_renewal(self):
            return ([{"entry": "valid"}], {"checksum": "sha256:" + "c" * 64}, preimage)

    def registry(**kwargs: object) -> dict[str, object]:
        seen["registry_publish"] = Path(str(kwargs["object_store_root"]))
        return {"selected_model_count": 13, "registry": None, "packages": []}

    monkeypatch.setattr(refresh, "capture_scheduler_provider_preimage", capture)
    monkeypatch.setattr(refresh, "load_canonical_readiness_entries_for_renewal", readiness)
    monkeypatch.setattr(refresh, "FileStateSnapshotIndexRepository", Repository)
    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", registry)

    receipt = refresh.refresh_scheduler_file_providers(config, dry_run=True)

    assert receipt["outcome"] == "dry_run"
    assert seen["registry_capture"] == config.provider_store_root
    assert seen["registry_publish"] == config.object_store_root
    assert seen["readiness"] == config.object_store_root
    assert seen["state"] == config.object_store_root


@pytest.mark.parametrize("lane", ["readiness", "state"])
def test_full_refresh_preserves_newer_authoritative_provider_on_snapshot_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    config = _config(tmp_path)
    _seed_empty_provider_files(config)
    authoritative: dict[str, bytes] = {}

    def publish_registry(**kwargs: object) -> dict[str, object]:
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    if lane == "readiness":
        real_loader = refresh.load_canonical_readiness_entries_for_renewal

        def load_then_replace(*args: object, **kwargs: object):
            snapshot = real_loader(*args, **kwargs)
            publish_canonical_readiness_index(
                [],
                config.readiness_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                generated_at=refresh.datetime.now(refresh.UTC) + timedelta(seconds=1),
            )
            authoritative["bytes"] = Path(config.readiness_uri).read_bytes()
            return snapshot

        monkeypatch.setattr(refresh, "load_canonical_readiness_entries_for_renewal", load_then_replace)
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
    readiness_before = Path(config.readiness_uri).read_bytes()

    def publish_registry(**kwargs: object) -> dict[str, object]:
        result = publish_scheduler_registry_manifest(
            [],
            kwargs["registry_manifest"],
            object_store_root=kwargs["object_store_root"],
            object_store_prefix=kwargs["object_store_prefix"],
            expected_preimage=kwargs["expected_preimage"],
        )
        return {"selected_model_count": 0, "registry": result, "packages": []}

    monkeypatch.setattr(refresh, "publish_all_basin_scheduler_registry", publish_registry)
    real_capture = provider_atomic_module.capture_provider_preimage
    readiness_capture_count = 0

    def fail_readiness_postread(path: Path, *args: object, **kwargs: object) -> ProviderPreimage:
        nonlocal readiness_capture_count
        if Path(path) == Path(config.readiness_uri):
            readiness_capture_count += 1
            if readiness_capture_count == 4:
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
            {**provider, "name": "readiness"},
            {**provider, "name": "state"},
        ],
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


def test_concurrent_receipt_publishers_keep_exact_newest_32(tmp_path: Path) -> None:
    root = tmp_path / "receipts"
    root.mkdir(mode=0o700)
    receipts = [
        refresh._receipt(
            run_id=f"refresh_{index:02d}",
            started=refresh.datetime(2026, 7, 14, index // 60, index % 60, tzinfo=refresh.UTC),
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=[],
        )
        for index in range(40)
    ]
    barrier = threading.Barrier(len(receipts))
    errors: list[BaseException] = []

    def publish(receipt: dict[str, object]) -> None:
        try:
            barrier.wait()
            refresh._publish_primary_receipt(root, receipt)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(receipt,)) for receipt in receipts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert json.loads((root / "latest.json").read_text())["run_id"] == "refresh_39"
    assert {path.stem for path in (root / "history").iterdir()} == {
        f"refresh_{index:02d}" for index in range(8, 40)
    }


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


def test_refresh_preflight_requires_private_euid_owned_lock_parent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.refresh_lock.parent.chmod(0o755)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_refresh_preflight_rejects_lock_parent_owned_by_another_euid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    owner = config.refresh_lock.parent.stat().st_uid
    monkeypatch.setattr(refresh.os, "geteuid", lambda: owner + 1)

    with pytest.raises(refresh.RefreshError) as error_info:
        refresh._preflight_config(config)

    assert error_info.value.reason == "configuration_invalid"


def test_config_rejects_database_or_relative_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        "NHMS_BASINS_ROOT": str(tmp_path / "Basins"),
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "registry.json"),
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": str(tmp_path / "readiness.json"),
        "NHMS_SCHEDULER_STATE_INDEX": str(tmp_path / "state.json"),
        "OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": str(tmp_path / "objects"),
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": str(tmp_path / "work"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": str(tmp_path / "emergency"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": str(tmp_path / "refresh"),
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        monkeypatch.setenv(selector, "redacted")
        with pytest.raises(refresh.RefreshError):
            refresh.RefreshConfig.from_env()
        monkeypatch.delenv(selector)
    monkeypatch.setenv("NHMS_BASINS_ROOT", "relative/Basins")
    with pytest.raises(refresh.RefreshError):
        refresh.RefreshConfig.from_env()


def test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent() -> None:
    root = Path(__file__).resolve().parents[1]
    service = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.service").read_text()
    timer = (root / "infra/systemd/nhms-scheduler-file-provider-refresh.timer").read_text()
    environment = (root / "infra/env/compute.scheduler-provider-refresh.env.example").read_text()
    wrapper = (root / "scripts/scheduler_file_provider_refresh_once.sh").read_text()
    installer = (root / "scripts/install_node22_scheduler_file_provider_refresh.sh").read_text()

    assert "ExecStart=/scratch/frd_muziyao/NWM/scripts/scheduler_file_provider_refresh_once.sh" in service
    assert "TimeoutStartSec=7200" in service
    assert "OnCalendar=*-*-* 02:15:00 UTC" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL" in service
    assert "PGPASSWORD" in service and "PGSSLROOTCERT" in service
    assert "nhms-compute-scheduler" not in service + timer
    for selector in ("DATABASE_URL=", "PIPELINE_DATABASE_URL=", "PGHOST=", "PGPORT="):
        assert selector not in environment
    assert "stat -c '%a'" in wrapper
    assert "DATABASE_URL PIPELINE_DATABASE_URL PGAPPNAME" in wrapper
    assert "cmp -s" in installer
    assert "scheduler_unchanged" in installer
    assert "rollback_files" in installer and "restore_refresh_state" in installer
    assert "--validate-current-receipt" in installer
    assert "assert_refresh_service_inactive" in installer
    assert 'unit_state "$timer"' in installer and 'unit_state "$service"' in installer
    assert 'restore_unit_state "$timer"' in installer and 'restore_unit_state "$service"' in installer
    assert "enable --now \"$timer\"" in installer
    assert "enable --now nhms-compute-scheduler" not in installer
    assert "Persistent=false" in timer
    for selector in refresh.LIBPQ_CONNECTION_ENV_KEYS:
        assert selector in service
        assert selector in wrapper
        assert selector in installer


def test_env_file_loader_rejects_duplicate_keys_and_cli_rejects_conflicting_operations(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "refresh.env"
    env_file.write_text("OBJECT_STORE_ROOT=/private\nOBJECT_STORE_ROOT=/other\n")

    with pytest.raises(refresh.RefreshError, match="configuration_invalid"):
        refresh._apply_environment_file(env_file)
    with pytest.raises(SystemExit):
        refresh._build_parser().parse_args(
            [
                "--recover-emergency",
                str(tmp_path / "emergency.json"),
                "--validate-current-receipt",
                str(tmp_path / "latest.json"),
            ]
        )


def test_installer_enable_lifecycle_and_failure_restore_with_fake_systemctl(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    source_units = root / "infra/systemd"
    units = repo / "infra/systemd"
    env_dir = repo / "infra/env"
    scripts_dir = repo / "scripts"
    units.mkdir(parents=True)
    env_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    shutil.copy2(root / "scripts/scheduler_file_provider_refresh.py", scripts_dir)
    for name in (
        "nhms-scheduler-file-provider-refresh.service",
        "nhms-scheduler-file-provider-refresh.timer",
    ):
        shutil.copy2(source_units / name, units / name)
    basins = tmp_path / "Basins"
    private_objects = tmp_path / "private-objects"
    shared_providers = tmp_path / "shared-providers"
    work = tmp_path / "private" / "work"
    receipts = tmp_path / "private" / "receipts"
    emergency = tmp_path / "private" / "emergency"
    for path in (basins, private_objects, shared_providers, work, receipts, emergency):
        path.mkdir(parents=True)
    for path in (tmp_path / "private", work, receipts, emergency):
        path.chmod(0o700)
    provider_paths = {
        "registry": shared_providers / "scheduler/registry/manifest-last.json",
        "readiness": shared_providers / "scheduler/canonical-readiness/index-last.json",
        "state": shared_providers / "scheduler/state-index/index-last.json",
    }
    providers = []
    for name, path in provider_paths.items():
        path.parent.mkdir(parents=True)
        path.write_text(name + "\n", encoding="utf-8")
        preimage = capture_scheduler_provider_preimage(path)
        providers.append(
            {
                "name": name,
                "before_sha256": preimage.sha256,
                "before_inode": preimage.inode,
                "before_schema_version": "v1",
                "before_generated_at": "2026-07-14T00:00:00Z",
                "before_payload_checksum": "sha256:" + "a" * 64,
                "after_sha256": preimage.sha256,
                "after_schema_version": "v1",
                "after_generated_at": "2026-07-14T01:00:00Z",
                "after_payload_checksum": "sha256:" + "b" * 64,
                "entry_count": 1,
            }
        )
    receipt = receipts / "latest.json"
    receipt.write_bytes(
        refresh._receipt_bytes(
            refresh._receipt(
                run_id="refresh_installer",
                started=refresh.datetime(2026, 7, 14, tzinfo=refresh.UTC),
                outcome="published",
                reason="success",
                phase="complete",
                providers=providers,
            )
        )
    )
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text(
        "\n".join(
            (
                f"NHMS_BASINS_ROOT={basins}",
                f"OBJECT_STORE_ROOT={private_objects}",
                f"NHMS_SCHEDULER_PROVIDER_STORE_ROOT={shared_providers}",
                "OBJECT_STORE_PREFIX=s3://nhms",
                f"NHMS_SCHEDULER_REGISTRY_MANIFEST={provider_paths['registry']}",
                f"NHMS_SCHEDULER_CANONICAL_READINESS_INDEX={provider_paths['readiness']}",
                f"NHMS_SCHEDULER_STATE_INDEX={provider_paths['state']}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT={work}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT={receipts}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT={emergency}",
                f"NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK={tmp_path / 'private/refresh'}",
            )
        )
        + "\n"
    )
    env_file.chmod(0o600)
    fake_state = tmp_path / "systemctl-state.json"
    fake_systemctl = tmp_path / "systemctl"
    fake_systemctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "path = os.environ['FAKE_SYSTEMCTL_STATE']\n"
        "try:\n"
        "    state = json.load(open(path, encoding='utf-8'))\n"
        "except FileNotFoundError:\n"
        "    state = {}\n"
        "args = [arg for arg in sys.argv[1:] if arg != '--user']\n"
        "command = args[0]\n"
        "unit = args[-1] if len(args) > 1 else ''\n"
        "with open(os.environ['FAKE_SYSTEMCTL_TRACE'], 'a', encoding='utf-8') as trace:\n"
        "    trace.write(' '.join(args) + '\\n')\n"
        "current = state.setdefault(unit, {'enabled':'disabled','active':'inactive'})\n"
        "if command == 'is-enabled': print(current['enabled'])\n"
        "elif command == 'is-active': print(current['active'])\n"
        "elif command == 'enable':\n"
        "    current['enabled'] = 'enabled'\n"
        "    if '--now' in args: current['active'] = 'active'\n"
        "elif command == 'disable':\n"
        "    current['enabled'] = 'disabled'\n"
        "    if '--now' in args: current['active'] = 'inactive'\n"
        "elif command == 'start': current['active'] = 'active'\n"
        "elif command == 'stop': current['active'] = 'inactive'\n"
        "elif command != 'daemon-reload': raise SystemExit(2)\n"
        "with open(path, 'w', encoding='utf-8') as handle: json.dump(state, handle)\n"
        "if os.environ.get('FAKE_FAIL_AFTER') == command + ':' + unit: raise SystemExit(9)\n"
    )
    fake_systemctl.chmod(0o755)
    fake_trace = tmp_path / "systemctl-trace.log"
    unit_dir = tmp_path / "units"
    state_root = tmp_path / "install-state"
    environment = {
        **os.environ,
        "FAKE_SYSTEMCTL_STATE": str(fake_state),
        "FAKE_SYSTEMCTL_TRACE": str(fake_trace),
        "NHMS_SCHEDULER_REFRESH_REPO": str(repo),
        "NHMS_SCHEDULER_REFRESH_UNIT_DIR": str(unit_dir),
        "NHMS_SCHEDULER_REFRESH_INSTALL_STATE_ROOT": str(state_root),
        "NHMS_SCHEDULER_REFRESH_SYSTEMCTL": str(fake_systemctl),
        "NHMS_SCHEDULER_REFRESH_PYTHON": sys.executable,
        "NHMS_SCHEDULER_REFRESH_RECEIPT": str(receipt),
        "PYTHONPATH": str(root),
    }
    installer = root / "scripts/install_node22_scheduler_file_provider_refresh.sh"

    subprocess.run([str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    enabled = subprocess.run(
        [str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )

    assert json.loads(enabled.stdout)["status"] == "enabled_active"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    assert state["nhms-scheduler-file-provider-refresh.service"]["active"] == "inactive"

    invalid_receipt = receipt.read_bytes()
    receipt.write_text('{"outcome":"published","database_free":true}\n')
    failed = subprocess.run(
        [str(installer), "--enable"], env=environment, check=False, capture_output=True, text=True
    )
    assert failed.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "enabled", "active": "active"}
    receipt.write_bytes(invalid_receipt)

    repeated = subprocess.run(
        [str(installer), "--enable"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(repeated.stdout)["status"] == "enabled_active"

    rolled_back = subprocess.run(
        [str(installer), "--rollback"], env=environment, check=True, capture_output=True, text=True
    )
    assert json.loads(rolled_back.stdout)["status"] == "rolled_back"
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.timer"] == {"enabled": "disabled", "active": "inactive"}
    assert state["nhms-compute-scheduler.service"] == {"enabled": "disabled", "active": "inactive"}

    subprocess.run([str(installer), "--install"], env=environment, check=True, capture_output=True, text=True)
    fail_environment = {
        **environment,
        "FAKE_FAIL_AFTER": "enable:nhms-scheduler-file-provider-refresh.timer",
    }
    failed_after_enable = subprocess.run(
        [str(installer), "--enable"], env=fail_environment, check=False, capture_output=True, text=True
    )
    assert failed_after_enable.returncode != 0
    state = json.loads(fake_state.read_text())
    assert state["nhms-scheduler-file-provider-refresh.timer"] == {"enabled": "disabled", "active": "inactive"}

    for transitional in ("activating", "deactivating", "reloading", "active"):
        state["nhms-scheduler-file-provider-refresh.service"] = {
            "enabled": "disabled",
            "active": transitional,
        }
        fake_state.write_text(json.dumps(state))
        fake_trace.write_text("")
        refused = subprocess.run(
            [str(installer), "--install"], env=environment, check=False, capture_output=True, text=True
        )
        assert refused.returncode != 0
        assert all(
            not line.startswith(("enable ", "disable ", "start ", "stop ", "daemon-reload"))
            for line in fake_trace.read_text().splitlines()
        )


def _write_wrapper_execution_fixture(tmp_path: Path, *, include_forbidden: bool = False) -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    env_dir = repo / "infra/env"
    interpreter = repo / ".venv/bin/python"
    env_dir.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    configured = {
        "NHMS_BASINS_ROOT": "/trusted/Basins",
        "OBJECT_STORE_ROOT": "/trusted/object-store",
        "NHMS_SCHEDULER_PROVIDER_STORE_ROOT": "/trusted/provider-store",
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": "/trusted/object-store/scheduler/registry/manifest-last.json",
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": "/trusted/object-store/scheduler/readiness/index-last.json",
        "NHMS_SCHEDULER_STATE_INDEX": "/trusted/object-store/scheduler/state/index-last.json",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": "/private/work",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": "/private/receipts",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": "/private/emergency",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": "/private/refresh",
    }
    lines = [f"{key}={value}" for key, value in configured.items()]
    if include_forbidden:
        lines.append("DATABASE_URL=must-not-load")
    env_file = env_dir / "compute.scheduler-provider-refresh.env"
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)
    marker = tmp_path / "interpreter-ran"
    interpreter.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {marker}\n"
        "printf 'BASINS=%s\\n' \"$NHMS_BASINS_ROOT\"\n"
        "printf 'OBJECTS=%s\\n' \"$OBJECT_STORE_ROOT\"\n"
        "printf 'PREFIX=%s\\n' \"$OBJECT_STORE_PREFIX\"\n"
        "printf 'REGISTRY=%s\\n' \"$NHMS_SCHEDULER_REGISTRY_MANIFEST\"\n"
        "printf 'READINESS=%s\\n' \"$NHMS_SCHEDULER_CANONICAL_READINESS_INDEX\"\n"
        "printf 'STATE=%s\\n' \"$NHMS_SCHEDULER_STATE_INDEX\"\n"
        "printf 'WORK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT\"\n"
        "printf 'RECEIPTS=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT\"\n"
        "printf 'EMERGENCY=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT\"\n"
        "printf 'LOCK=%s\\n' \"$NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK\"\n"
        "printf 'DATABASE_URL=%s\\n' \"${DATABASE_URL-<unset>}\"\n"
        "printf 'PGHOST=%s\\n' \"${PGHOST-<unset>}\"\n"
        "printf 'ARGS=%s\\n' \"$*\"\n"
    )
    interpreter.chmod(0o755)
    wrapper = tmp_path / "refresh-wrapper.sh"
    wrapper.write_text(
        (root / "scripts/scheduler_file_provider_refresh_once.sh")
        .read_text()
        .replace("repo=/scratch/frd_muziyao/NWM", f"repo={repo}")
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def test_wrapper_clean_environment_loads_fixed_config_and_strips_inherited_db_selectors(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(wrapper), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "DATABASE_URL": "inherited-secret",
            "PGHOST": "inherited-host",
        },
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert "BASINS=/trusted/Basins" in result.stdout
    assert "OBJECTS=/trusted/object-store" in result.stdout
    assert "PREFIX=s3://nhms" in result.stdout
    assert "REGISTRY=/trusted/object-store/scheduler/registry/manifest-last.json" in result.stdout
    assert "READINESS=/trusted/object-store/scheduler/readiness/index-last.json" in result.stdout
    assert "STATE=/trusted/object-store/scheduler/state/index-last.json" in result.stdout
    assert "WORK=/private/work" in result.stdout
    assert "RECEIPTS=/private/receipts" in result.stdout
    assert "EMERGENCY=/private/emergency" in result.stdout
    assert "LOCK=/private/refresh" in result.stdout
    assert "DATABASE_URL=<unset>" in result.stdout
    assert "PGHOST=<unset>" in result.stdout
    assert result.stdout.rstrip().endswith("scheduler_file_provider_refresh.py --dry-run")


def test_wrapper_rejects_forbidden_selector_in_mode_0600_env_before_exec(tmp_path: Path) -> None:
    wrapper, marker = _write_wrapper_execution_fixture(tmp_path, include_forbidden=True)

    result = subprocess.run(
        ["/bin/bash", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert result.returncode != 0
    assert not marker.exists()
