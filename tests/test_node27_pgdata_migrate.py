"""Public CLI/state/HBA/copy-boundary regressions for offline PGDATA relocation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID
from packages.common.node27_cold_tablespace_container import COLD_BIND, normalize_raw_inspect
from packages.common.node27_pgdata_host import Host, MigrationError, UNITS, path_identity
from packages.common.node27_pgdata_migrate import DEFAULTS, Migration, _hba_overlay
from scripts.node27_pgdata_migrate import main, parser


def _private(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "durable" / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    workspace.chmod(0o700)
    return workspace


def _inspect(
    *,
    name: str = "nhms-db",
    source: str = "/home/nwm/nhms-pgdata",
    port: str = "55432",
    running: bool = True,
    extra_bind: str | None = None,
) -> dict:
    binds = [f"{source}:/home/postgres/pgdata/data:rw"]
    if extra_bind:
        binds.append(extra_bind)
    return {
        "Id": "a" * 64,
        "Name": "/" + name,
        "Image": PINNED_IMAGE_ID,
        "State": {"Running": running, "Status": "running" if running else "exited"},
        "Config": {
            "Image": PINNED_IMAGE_ID,
            "Env": ["POSTGRES_PASSWORD=ultra-secret", "POSTGRES_USER=nhms", "PGDATA=/home/postgres/pgdata/data"],
            "Cmd": ["postgres"],
            "Entrypoint": None,
            "WorkingDir": "/",
            "User": "1000:1000",
            "Labels": {},
            "StopSignal": "SIGINT",
            "Healthcheck": None,
        },
        "HostConfig": {
            "Binds": binds,
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NanoCpus": 0,
            "Memory": 0,
            "ShmSize": 0,
            "StopTimeout": 10,
            "ReadonlyRootfs": False,
            "CapAdd": [],
            "CapDrop": [],
            "SecurityOpt": [],
            "NetworkMode": "bridge",
            "Privileged": False,
            "PublishAllPorts": False,
            "AutoRemove": False,
            "VolumesFrom": [],
            "Devices": [],
            "DeviceRequests": [],
            "Tmpfs": {},
            "ExtraHosts": [],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": item.split(":")[0],
                "Destination": item.split(":")[1],
                "RW": item.endswith(":rw"),
            }
            for item in binds
        ],
    }


class FakeHost(Host):
    def __init__(self, inspects: dict[str, dict], *, filesystem: str = "ext4") -> None:
        self.inspects = inspects
        self.filesystem_type = filesystem
        self.commands: list[tuple[str, ...]] = []
        self.helpers: list[str] = []

    def command(self, argv, *, timeout: int = 30, allow_failure: bool = False, max_bytes: int = 1024 * 1024):
        argv = tuple(argv)
        self.commands.append(argv)
        if argv[:4] == ("/usr/bin/docker", "inspect", "--type", "container"):
            identity = argv[4]
            if identity in self.inspects:
                return SimpleNamespace(returncode=0, stdout=json.dumps([self.inspects[identity]]), stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr=f"Error: No such container: {identity}")
        if argv[:3] == ("/usr/bin/findmnt", "--json", "--target"):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "filesystems": [
                            {"source": "/dev/sda1", "fstype": self.filesystem_type, "target": "/", "options": "rw"}
                        ]
                    }
                ),
                stderr="",
            )
        if argv[:3] == ("/usr/bin/docker", "ps", "-q"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    def helper(self, state, script, *, target: bool = False, payload=None, timeout: int = 120):
        self.helpers.append(script)
        raise AssertionError("helper must not run during planning")


def test_parser_defaults_to_plan_and_exposes_frozen_shared_flags() -> None:
    flags = {action.option_strings[0] for action in parser()._actions if action.option_strings}
    assert flags >= {
        "--action",
        "--workspace",
        "--source-container",
        "--source-pgdata",
        "--target-pgdata",
        "--reserve-bytes",
        "--mdadm-evidence",
        "--smart-evidence",
        "--enforce",
        "--disposable-root",
        "--database",
        "--admin-role",
        "--reader-dsn-file",
        "--writer-dsn-file",
        "--drain-timeout",
    }
    args = parser().parse_args(["--workspace", "/data/GHDC/nhms-pgdata-ops"])
    assert args.action == "plan" and args.enforce is False
    assert DEFAULTS["admin_role"] == "nhms"


def test_default_plan_reports_without_stopping_or_creating_state(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    host = FakeHost({"nhms-db": _inspect()})
    result = Migration(workspace, host=host).run(
        "plan",
        overrides={
            "reader_dsn_file": str(
                _private(
                    tmp_path / "reader.dsn", "host=127.0.0.1 port=55432 dbname=nhms user=nhms_display_ro password=x"
                )
            ),
            "writer_dsn_file": str(
                _private(
                    tmp_path / "writer.dsn", "host=127.0.0.1 port=55432 dbname=nhms user=nhms_ingest_rw password=y"
                )
            ),
        },
    )
    assert result["mutation"] is False
    assert result["action"] == "plan"
    assert not (workspace / "state.json").exists()
    assert not any(command[1] in {"stop", "run", "create", "update", "exec"} for command in host.commands)
    assert not host.helpers


def test_mutating_action_without_enforce_refuses_before_lock_or_state(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    host = FakeHost({"nhms-db": _inspect()})
    with pytest.raises(MigrationError, match="--enforce"):
        Migration(workspace, host=host).run("prepare", overrides={})
    assert not (workspace / "state.json").exists()
    assert host.commands == []


def test_out_of_order_activate_and_release_refuse_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr("packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock", lambda: _nullcontext())
    state = {
        "schema_version": 1,
        "stage": "prepared",
        "writes_released": False,
        "operation": "ab" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
        "config": dict(DEFAULTS),
    }
    _private(workspace / "state.json", json.dumps(state))
    host = FakeHost({})
    for action in ("activate", "release"):
        with pytest.raises(MigrationError, match="state/action pair refused"):
            Migration(workspace, host=host).run(action, enforce=True)
    assert json.loads((workspace / "state.json").read_text())["stage"] == "prepared"
    assert host.commands == []


def test_marked_release_forbids_stale_rollback_and_allows_forward_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr("packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock", lambda: _nullcontext())
    state = {
        "schema_version": 1,
        "stage": "release_intent",
        "writes_released": True,
        "operation": "cd" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
        "config": dict(DEFAULTS),
    }
    _private(workspace / "state.json", json.dumps(state))
    host = FakeHost({})
    with pytest.raises(MigrationError, match="forward-only"):
        Migration(workspace, host=host).run("rollback", enforce=True)
    with pytest.raises(MigrationError, match="forward-only"):
        Migration(workspace, host=host).run("copy", enforce=True)
    loaded = json.loads((workspace / "state.json").read_text())
    assert loaded["writes_released"] is True
    assert loaded["stage"] == "release_intent"
    assert host.commands == []


def test_unrecorded_named_container_is_preserved_not_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr("packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock", lambda: _nullcontext())
    original = _inspect(running=False)
    stranger = _inspect(name="nhms-db")
    stranger["Id"] = "b" * 64
    state = {
        "schema_version": 1,
        "stage": "activate_intent",
        "writes_released": False,
        "operation": "ef" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
        "config": dict(DEFAULTS),
        "original": original,
        "original_id": original["Id"],
        "original_config_digest": normalize_raw_inspect(original).config_digest,
        "source_identity": [1, 2, os.getuid(), os.getgid(), 0o700],
        "target_parent_identity": [1, 3, os.getuid(), os.getgid(), 0o700],
        "units": {},
        "foreign_hold": {},
        "candidate_id": None,
        "backup_name": "nhms-db-pgdata-original-" + "ef" * 16,
    }
    _private(workspace / "state.json", json.dumps(state))
    host = FakeHost({original["Id"]: original, "nhms-db": stranger})
    host.foreign_hold = lambda config: {}  # type: ignore[method-assign]
    host.verify_units = lambda *args, **kwargs: None  # type: ignore[method-assign]
    real_identity = path_identity
    monkeypatch.setattr(
        "packages.common.node27_pgdata_migrate.path_identity",
        lambda path: (
            state["source_identity"]
            if str(path) == "/home/nwm/nhms-pgdata"
            else state["target_parent_identity"]
            if str(path) == "/data/GHDC"
            else real_identity(path)
        ),
    )
    with pytest.raises(MigrationError, match="unrecorded candidate is preserved"):
        Migration(workspace, host=host).run("rollback", enforce=True)
    assert not any(command[1] in {"rm", "rename", "stop"} for command in host.commands)


def test_hba_overlay_rejects_every_login_except_verified_reader_and_local_admin() -> None:
    original = b"host all all 0.0.0.0/0 md5\nlocal all postgres peer\n"
    overlay = _hba_overlay(
        {
            "operation": "aa" * 16,
            "config": {"admin_role": "nhms"},
            "credentials": {"reader": {"user": "nhms_display_ro"}},
            "catalog": {"roles": ["nhms", "nhms_display_ro", "nhms_ingest_rw", "nhms_download_rw"]},
        },
        original,
    )
    text = overlay.decode()
    assert text.endswith(original.decode())
    assert 'host all "nhms_ingest_rw" 0.0.0.0/0 reject' in text
    assert 'host all "nhms_download_rw" 0.0.0.0/0 reject' in text
    assert 'local all "nhms_ingest_rw" reject' in text
    assert 'local all "nhms" reject' not in text
    assert 'host all "nhms_display_ro" 0.0.0.0/0 reject' not in text
    assert 'local all "nhms_display_ro" reject' in text
    assert "default_transaction_read_only" not in text
    assert original.decode() in text


def test_disposable_identity_cannot_select_live_paths(tmp_path: Path) -> None:
    from packages.common.node27_pgdata_migrate import _validate_paths

    token = "ab" * 16
    root = tmp_path / f"nhms-pgdata-oracle-{token}"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    source.chmod(0o700)
    target = root / "target"
    inspect = _inspect(name=root.name + "-db", source=str(source), port="55494")
    with pytest.raises(MigrationError, match="production"):
        _validate_paths(
            workspace,
            {
                **DEFAULTS,
                "source_container": inspect["Name"].lstrip("/"),
                "source_pgdata": str(source),
                "target_pgdata": str(target),
                "disposable_root": None,
            },
            inspect,
        )
    with pytest.raises(MigrationError, match="production"):
        _validate_paths(
            workspace,
            {
                **DEFAULTS,
                "source_container": "nhms-db",
                "source_pgdata": "/home/nwm/nhms-pgdata",
                "target_pgdata": "/home/nwm/nhms-pgdata-new",
                "disposable_root": None,
            },
            _inspect(),
        )
    inspect["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "55432"
    inspect["Name"] = "/" + root.name + "-db"
    with pytest.raises(MigrationError, match="production"):
        _validate_paths(
            workspace,
            {
                **DEFAULTS,
                "source_container": root.name + "-db",
                "source_pgdata": str(source),
                "target_pgdata": str(target),
                "disposable_root": str(root),
            },
            inspect,
        )


def test_cold_bind_is_refused_as_relocation_identity(tmp_path: Path) -> None:
    from packages.common.node27_pgdata_migrate import _validate_paths

    workspace = _workspace(tmp_path)
    inspect = _inspect(extra_bind=COLD_BIND)
    with pytest.raises(MigrationError, match="cold identity"):
        _validate_paths(workspace, dict(DEFAULTS), inspect)


def test_ephemeral_workspace_is_refused(tmp_path: Path) -> None:
    host = FakeHost({"nhms-db": _inspect()}, filesystem="tmpfs")
    with pytest.raises(MigrationError, match="ephemeral"):
        host.durable_workspace(Path("/tmp/nhms-pgdata-ops"))
    workspace = _workspace(tmp_path)
    with pytest.raises(MigrationError, match="durable"):
        host.durable_workspace(workspace)


def test_writes_released_cannot_revert(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    engine = Migration(workspace, host=FakeHost({}))
    engine.state = {
        "schema_version": 1,
        "stage": "released",
        "writes_released": False,
        "operation": "11" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
    }
    _private(workspace / "state.json", json.dumps({**engine.state, "writes_released": True}))
    with pytest.raises(MigrationError, match="monotonic"):
        engine.save()


def test_cli_plan_json_contains_no_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    host = FakeHost({"nhms-db": _inspect()})
    monkeypatch.setattr("scripts.node27_pgdata_migrate.Migration", lambda path: Migration(path, host=host))
    assert main(["--workspace", str(workspace)]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True and payload["mutation"] is False
    assert "ultra-secret" not in captured.out + captured.err
    assert "password" not in captured.out.lower()


def test_fixed_fence_set_covers_sixteen_persistent_units() -> None:
    assert len(UNITS) == 16
    assert "nhms-node27-timeseries-compression-replay.service" in UNITS
    assert "nhms-display-api.service" in UNITS
    assert "nhms-node27-resource-governance.timer" in UNITS
    assert "nhms-node27-cold-residency.service" not in UNITS


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()
