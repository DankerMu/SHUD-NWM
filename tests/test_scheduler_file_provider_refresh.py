from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from packages.common import provider_atomic as provider_atomic_module
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    capture_provider_preimage,
    provider_destination_lock,
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
    publisher()
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
    assert json.loads(emergency[0].read_text())["providers"][2]["after_sha256"] == "f" * 64


def test_primary_and_emergency_receipt_failure_is_replace_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _stub_provider_pipeline(monkeypatch)
    monkeypatch.setattr(refresh, "_publish_primary_receipt", lambda *args: (_ for _ in ()).throw(OSError()))

    def fail_emergency(fd: int, receipt: object) -> None:
        del receipt
        os.close(fd)
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
    with pytest.raises(ValueError):
        refresh._validate_receipt(
            {**receipt, "orphans": {"items": [], "total": refresh.MAX_ORPHANS + 1, "truncated": True}}
        )
    with pytest.raises(ValueError):
        refresh._validate_receipt({**receipt, "residues": ["../outside"]})
    with pytest.raises(ValueError):
        refresh._receipt_bytes({"padding": "x" * refresh.MAX_RECEIPT_BYTES})


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


def test_config_rejects_database_or_relative_runtime_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = {
        "NHMS_BASINS_ROOT": str(tmp_path / "Basins"),
        "NHMS_SCHEDULER_REGISTRY_MANIFEST": str(tmp_path / "registry.json"),
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX": str(tmp_path / "readiness.json"),
        "NHMS_SCHEDULER_STATE_INDEX": str(tmp_path / "state.json"),
        "OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "OBJECT_STORE_PREFIX": "s3://nhms",
        "NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT": str(tmp_path / "work"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT": str(tmp_path / "receipts"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT": str(tmp_path / "emergency"),
        "NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK": str(tmp_path / "refresh"),
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABASE_URL", "redacted")
    with pytest.raises(refresh.RefreshError):
        refresh.RefreshConfig.from_env()
    monkeypatch.delenv("DATABASE_URL")
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
    assert (
        "UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL PGHOST PGPORT PGDATABASE PGUSER PGSERVICE PGSERVICEFILE"
        in service
    )
    assert "nhms-compute-scheduler" not in service + timer
    for selector in ("DATABASE_URL=", "PIPELINE_DATABASE_URL=", "PGHOST=", "PGPORT="):
        assert selector not in environment
    assert "stat -c '%a'" in wrapper
    assert "DATABASE_URL|PIPELINE_DATABASE_URL|PGHOST|PGPORT|PGDATABASE|PGUSER|PGSERVICE|PGSERVICEFILE" in wrapper
    assert "cmp -s" in installer
    assert "scheduler_unchanged" in installer
    assert "rollback_files" in installer and "restore_refresh_state" in installer
    assert 'unit_state "$timer"' in installer and 'unit_state "$service"' in installer
    assert 'restore_unit_state "$timer"' in installer and 'restore_unit_state "$service"' in installer
    assert "enable --now \"$timer\"" in installer
    assert "enable --now nhms-compute-scheduler" not in installer


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
