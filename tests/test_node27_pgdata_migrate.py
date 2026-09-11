"""Public CLI/state/HBA/copy-boundary regressions for offline PGDATA relocation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID
from packages.common.node27_cold_tablespace_container import normalize_raw_inspect
from packages.common.node27_pgdata_host import DISPLAY, FENCE, UNITS, Host, MigrationError, covered_tree, path_identity
from packages.common.node27_pgdata_migrate import DEFAULTS, Migration
from packages.common.safe_fs import SafeFilesystemError
from scripts.node27_pgdata_migrate import main


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
            "User": f"{os.getuid()}:{os.getgid()}",
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

    def durable_workspace(self, path: Path) -> list[int]:
        # Synthetic journal IO is independent of pytest's physical /tmp placement.
        # Production path policy is exercised explicitly below, not overridden globally.
        return path_identity(path)

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


def test_default_plan_reports_without_stopping_or_creating_state(tmp_path: Path) -> None:
    root = tmp_path / ("nhms-pgdata-oracle-" + "01" * 16)
    root.mkdir(mode=0o700)
    source, workspace = root / "source", root / "workspace"
    source.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    name = root.name + "-db"
    host = FakeHost({name: _inspect(name=name, source=str(source), port="55494")})
    result = Migration(workspace, host=host).run(
        overrides={
            "source_container": name,
            "source_pgdata": str(source),
            "target_pgdata": str(root / "target"),
            "disposable_root": str(root),
            "reserve_bytes": 1,
            "reader_dsn_file": str(
                _private(root / "reader.dsn", "host=127.0.0.1 port=55494 dbname=nhms user=nhms_display_ro password=x")
            ),
            "writer_dsn_file": str(
                _private(root / "writer.dsn", "host=127.0.0.1 port=55494 dbname=nhms user=nhms_ingest_rw password=y")
            ),
        }
    )
    assert result["mutation"] is False and result["action"] == "plan" and result["blockers"] == []
    assert not (workspace / "state.json").exists()
    assert not (root / "lifecycle.lock").exists()
    assert host.inspects[name]["State"]["Running"]
    assert all(command[1] == "inspect" for command in host.commands)
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
    assert all(command[:3] == ("/usr/bin/findmnt", "--json", "--target") for command in host.commands)


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
    assert all(command[:3] == ("/usr/bin/findmnt", "--json", "--target") for command in host.commands)


def test_unrecorded_named_container_is_preserved_not_owned(tmp_path: Path) -> None:
    root = tmp_path / ("nhms-pgdata-oracle-" + "ef" * 16)
    root.mkdir(mode=0o700)
    workspace, source, parent = (root / name for name in ("workspace", "source", "target-parent"))
    for path in (workspace, source, parent):
        path.mkdir(mode=0o700)
    name = root.name + "-db"
    original = _inspect(name=name, source=str(source), running=False, port="55494")
    stranger = _inspect(name=name, source=str(source), port="55494")
    stranger["Id"] = "b" * 64
    config = {
        **DEFAULTS,
        "source_container": name,
        "source_pgdata": str(source),
        "target_pgdata": str(parent / "pgdata"),
        "disposable_root": str(root),
        "reserve_bytes": 1,
    }
    host = FakeHost({original["Id"]: original, name: stranger})
    credentials = {}
    for kind, role in (("reader", "nhms_display_ro"), ("writer", "nhms_ingest_rw")):
        dsn = f"host=127.0.0.1 port=55494 dbname=nhms user={role} password=secret"
        config[kind + "_dsn_file"] = str(_private(root / (kind + ".dsn"), dsn))
        credentials[kind] = {
            "host": "127.0.0.1",
            "port": "55494",
            "dbname": "nhms",
            "user": role,
            "digest": hashlib.sha256(dsn.encode()).hexdigest(),
        }
    state = {
        "schema_version": 1,
        "stage": "rollback_intent",
        "writes_released": False,
        "operation": "ef" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
        "config": config,
        "original": original,
        "original_id": original["Id"],
        "original_config_digest": normalize_raw_inspect(original).config_digest,
        "source_identity": path_identity(source),
        "target_parent_identity": path_identity(parent),
        "units": {},
        "host_identity": host.identity(),
        "candidate_id": None,
        "credentials": credentials,
        "original_policy_restore_intent": True,
        "backup_name": name + "-pgdata-original-" + "ef" * 16,
    }
    _private(workspace / "state.json", json.dumps(state))
    with pytest.raises(MigrationError, match="unrecorded candidate is preserved"):
        Migration(workspace, host=host).run("rollback", enforce=True)
    assert not any(command[1] in {"rm", "rename", "stop", "update", "start"} for command in host.commands)


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

    root = tmp_path / ("nhms-pgdata-oracle-" + "12" * 16)
    root.mkdir(mode=0o700)
    source, workspace = root / "source", root / "workspace"
    source.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    config = {
        **DEFAULTS,
        "source_container": root.name + "-db",
        "source_pgdata": str(source),
        "target_pgdata": str(root / "target"),
        "disposable_root": str(root),
        "reserve_bytes": 1,
    }
    inspect = _inspect(
        name=config["source_container"],
        source=str(source),
        port="55494",
        extra_bind=f"{root / 'nhms-cold-tablespace'}:/nhms_cold:rw",
    )
    with pytest.raises(MigrationError, match="cold identity"):
        _validate_paths(workspace, config, inspect)


def test_ephemeral_workspace_is_refused() -> None:
    host = FakeHost({})
    with pytest.raises(MigrationError):
        Host.durable_workspace(host, Path("/tmp/nhms-pgdata-ops"))
    assert host.commands == []


def test_tmpfs_workspace_is_refused_on_durable_lexical_path(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    modeled = Path("/owned-durable-fixture/workspace")
    real_identity = path_identity(workspace)
    monkeypatch.setattr("packages.common.node27_pgdata_host.path_identity", lambda path: real_identity)
    host = FakeHost({}, filesystem="tmpfs")
    with pytest.raises(MigrationError):
        Host.durable_workspace(host, modeled)
    assert host.commands[0][3] == str(modeled)


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


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()


def test_cli_sanitizes_unreadable_and_malformed_private_state(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    host = FakeHost({})
    monkeypatch.setattr("scripts.node27_pgdata_migrate.Migration", lambda path: Migration(path, host=host))
    _private(workspace / "state.json", '{"password":"secret-dsn"')
    assert main(["--workspace", str(workspace), "--action", "prepare", "--enforce"]) == 2
    result = capsys.readouterr()
    assert json.loads(result.err)["ok"] is False
    assert "secret-dsn" not in result.err and "Traceback" not in result.err
    (workspace / "state.json").unlink()
    assert main(["--workspace", str(workspace / "missing"), "--action", "prepare", "--enforce"]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_first_prepare_requires_operator_reserve_before_fencing(tmp_path: Path) -> None:
    root = tmp_path / ("nhms-pgdata-oracle-" + "34" * 16)
    root.mkdir(mode=0o700)
    source, workspace = root / "source", root / "workspace"
    source.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    name = root.name + "-db"
    host = FakeHost({name: _inspect(name=name, source=str(source), port="55494")})
    with pytest.raises(MigrationError, match="reserve"):
        Migration(workspace, host=host).run(
            "prepare",
            enforce=True,
            overrides={
                "source_container": name,
                "source_pgdata": str(source),
                "target_pgdata": str(root / "target"),
                "disposable_root": str(root),
            },
        )
    assert not (workspace / "state.json").exists()
    assert host.inspects[name]["State"]["Running"]
    assert all(command[1] == "inspect" for command in host.commands)


class UnitHost(FakeHost):
    def __init__(self, units: dict[str, dict[str, str]]):
        super().__init__({})
        self.units = units

    def command(self, argv, **kwargs):
        if list(argv[:3]) != ["/usr/bin/systemctl", "--user", "show"]:
            if list(argv[:2]) == ["/usr/bin/systemctl", "--user"]:
                self.commands.append(tuple(argv))
                if argv[2] in {"start", "stop"}:
                    self.units[argv[3]]["ActiveState"] = "active" if argv[2] == "start" else "inactive"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return super().command(argv, **kwargs)
        name = argv[3]
        value = dict(self.units.get(name, {"LoadState": "not-found", "ActiveState": "inactive"}))
        if name in self.units and name in UNITS:
            fence = self.fence_path(name)
            if fence.exists():
                value["DropInPaths"] = (value.get("DropInPaths", "") + " " + str(fence)).strip()
        return SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in value.items()), stderr="")


@pytest.fixture
def runtime_checkout(tmp_path: Path) -> Path:
    runtime = tmp_path.resolve() / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / ".gitignore").write_text(".venv/\n")
    (runtime / "app.py").write_text("print('approved runtime')\n")
    host = Host()
    host.command(["/usr/bin/git", "init", str(runtime)])
    host.command(["/usr/bin/git", "-C", str(runtime), "add", ".gitignore", "app.py"])
    host.command(
        [
            "/usr/bin/git",
            "-C",
            str(runtime),
            "-c",
            "user.name=Runtime Fixture",
            "-c",
            "user.email=runtime@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "Approved runtime",
        ]
    )
    return runtime


def test_runtime_virtualenv_link_preserves_code_identity(runtime_checkout: Path) -> None:
    host = Host()
    plain = host.runtime_identity(runtime_checkout)
    assert "virtualenv_link" not in plain
    target = runtime_checkout.parent / "shared-venv"
    target.mkdir()
    target.chmod(0o775)
    (runtime_checkout / ".venv").symlink_to(target, target_is_directory=True)
    admitted = host.runtime_identity(runtime_checkout)
    assert "virtualenv_link" in admitted
    assert admitted["head"] == plain["head"]
    assert admitted["tree_digest"] == plain["tree_digest"]
    assert target.stat().st_mode & 0o777 == 0o775

    # Package activity is not a new package-integrity or timestamp contract.
    (target / "installed-package").write_text("package contents\n")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    assert host.runtime_identity(runtime_checkout) == admitted

    (runtime_checkout / "app.py").write_text("print('changed runtime')\n")
    changed = host.runtime_identity(runtime_checkout)
    assert changed["tree_digest"] != admitted["tree_digest"]
    assert changed["virtualenv_link"] == admitted["virtualenv_link"]
    (runtime_checkout / "untracked.py").write_text("print('local runtime code')\n")
    assert host.runtime_identity(runtime_checkout)["tree_digest"] != changed["tree_digest"]


@pytest.mark.parametrize("drift", ("retarget", "replace-link", "replace-target", "target-mode"))
def test_runtime_virtualenv_drift_refuses_unit_verification(runtime_checkout: Path, monkeypatch, drift: str) -> None:
    monkeypatch.setattr(Path, "home", lambda: runtime_checkout.parent)
    target = runtime_checkout.parent / "shared-venv"
    target.mkdir(mode=0o775)
    target.chmod(0o775)
    link = runtime_checkout / ".venv"
    link.symlink_to(target, target_is_directory=True)
    name = "nhms-node27-download.service"

    class GitUnitHost(UnitHost):
        def command(self, argv, **kwargs):
            if argv[0] == "/usr/bin/git":
                return Host.command(self, argv, **kwargs)
            return super().command(argv, **kwargs)

    host = GitUnitHost(
        {
            name: {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "UnitFileState": "static",
                "Type": "oneshot",
                "WorkingDirectory": str(runtime_checkout),
                "ExecStart": f"{runtime_checkout}/.venv/bin/python app.py",
                "EnvironmentFiles": "",
                "FragmentPath": "",
                "DropInPaths": "",
            }
        }
    )
    state = {"config": DEFAULTS, "units": host.units_snapshot(DEFAULTS)}
    frozen = state["units"][name]["runtime"]
    host.verify_units(state)
    if drift in {"retarget", "replace-link"}:
        # Keep the old inode alive so replacement cannot reuse it.
        link.rename(runtime_checkout.parent / "old-venv-link")
        if drift == "retarget":
            target = runtime_checkout.parent / "other-venv"
            target.mkdir()
        link.symlink_to(target, target_is_directory=True)
    elif drift == "replace-target":
        target.rename(runtime_checkout.parent / "old-venv-directory")
        target.mkdir()
    else:
        target.chmod(0o700)
    changed = host.runtime_identity(runtime_checkout)
    assert changed["tree_digest"] == frozen["tree_digest"]
    assert changed["virtualenv_link"] != frozen["virtualenv_link"]
    with pytest.raises(MigrationError):
        host.verify_units(state)


@pytest.mark.parametrize("unsafe", ("ordinary-code", "tracked-venv", "dangling-venv", "file-venv", "indirect-venv"))
def test_runtime_identity_refuses_non_environment_symlinks(runtime_checkout: Path, unsafe: str) -> None:
    target = runtime_checkout.parent / "external"
    if unsafe in {"ordinary-code", "file-venv"}:
        target.write_text("external bytes\n")
    elif unsafe in {"tracked-venv", "indirect-venv"}:
        target.mkdir()
    if unsafe == "indirect-venv":
        (target / "env").mkdir()
        alias = runtime_checkout.parent / "external-alias"
        alias.symlink_to(target, target_is_directory=True)
        target = alias / "env"
    link = runtime_checkout / ("linked.py" if unsafe == "ordinary-code" else ".venv")
    link.symlink_to(target)
    host = Host()
    if unsafe == "tracked-venv":
        host.command(["/usr/bin/git", "-C", str(runtime_checkout), "add", "--force", ".venv"])
    with pytest.raises((MigrationError, SafeFilesystemError, OSError)):
        host.runtime_identity(runtime_checkout)


def test_persistent_fence_preserves_optional_foreign_hold_and_original_timer_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = _workspace(tmp_path)
    held, active = "nhms-node27-autopipe.timer", "nhms-node27-timeseries-retention.timer"
    units = {}
    for name, running in ((held, "inactive"), (active, "active")):
        fragment = _private(tmp_path / name, "[Timer]\nOnUnitActiveSec=60\n")
        units[name] = {
            "LoadState": "loaded",
            "ActiveState": running,
            "SubState": "waiting",
            "UnitFileState": "enabled",
            "Type": "",
            "WorkingDirectory": "",
            "ExecStart": "",
            "EnvironmentFiles": "",
            "FragmentPath": str(fragment),
            "DropInPaths": "",
        }
    foreign = tmp_path / ".config/systemd/user" / (held + ".d") / "91-foreign-capacity.conf"
    foreign.parent.mkdir(parents=True)
    foreign_bytes = "[Unit]\nConditionPathExists=/operator/approval-still-absent\n"
    _private(foreign, foreign_bytes)
    units[held]["DropInPaths"] = str(foreign)
    host = UnitHost(units)
    state = {
        "workspace": str(workspace),
        "operation": "56" * 16,
        "stage": "prepared",
        "writes_released": False,
        "config": {**DEFAULTS, "drain_timeout": 1},
        "units": host.units_snapshot(DEFAULTS),
        "display_unfenced": False,
    }
    host.install_fences(state)
    assert host.fence_path(held).name == FENCE
    assert host.fence_path(held).exists() and host.fence_path(active).exists()
    # A fresh host object must honor the persistent files, not in-process memory.
    restored = UnitHost(units)
    restored.verify_units(state, required_fences=True)
    restored.restore_units(state)
    assert foreign.read_text() == foreign_bytes
    assert units[held]["ActiveState"] == "inactive"
    assert units[active]["ActiveState"] == "active"
    assert not host.fence_path(held).exists() and not host.fence_path(active).exists()
    assert ("/usr/bin/systemctl", "--user", "start", held) not in restored.commands


def test_foreign_fence_change_refuses_before_owned_fence_removal(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = _workspace(tmp_path)
    name = "nhms-node27-download.timer"
    fragment = _private(tmp_path / name, "[Timer]\nOnUnitActiveSec=60\n")
    units = {
        name: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "UnitFileState": "enabled",
            "Type": "",
            "FragmentPath": str(fragment),
            "DropInPaths": "",
            "EnvironmentFiles": "",
        }
    }
    host = UnitHost(units)
    state = {
        "workspace": str(workspace),
        "operation": "78" * 16,
        "stage": "prepared",
        "writes_released": False,
        "config": {**DEFAULTS, "drain_timeout": 1},
        "units": host.units_snapshot(DEFAULTS),
    }
    host.install_fences(state)
    fragment.write_text("[Timer]\nOnUnitActiveSec=1\n")
    with pytest.raises(MigrationError, match="foreign runtime/unit"):
        UnitHost(units).restore_units(state)
    assert host.fence_path(name).exists()
    assert units[name]["ActiveState"] == "inactive"


def test_governance_target_exception_does_not_unbind_other_caller_configuration(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = _workspace(tmp_path)
    name = "nhms-node27-resource-governance.service"
    source, target = str(tmp_path / "source"), str(tmp_path / "target")
    config = {**DEFAULTS, "source_pgdata": source, "target_pgdata": target, "drain_timeout": 1}
    environment = _private(tmp_path / "governance.env", f"NODE27_GOVERNANCE_PGDATA_ROOT={source}\nLIMIT=7\n")
    fragment = _private(tmp_path / name, "[Service]\nType=oneshot\n")
    units = {
        name: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "UnitFileState": "static",
            "Type": "oneshot",
            "FragmentPath": str(fragment),
            "DropInPaths": "",
            "EnvironmentFiles": "",
            "ExecStart": f"/bin/bash -lc '. {environment}; exec /old/runtime/governance'",
        }
    }
    host = UnitHost(units)
    state = {
        "workspace": str(workspace),
        "operation": "90" * 16,
        "stage": "activated_readonly",
        "writes_released": False,
        "config": config,
        "units": host.units_snapshot(config),
    }
    host.install_fences(state)
    with pytest.raises(MigrationError, match="governance PGDATA target"):
        host.release_callers_ready(state)
    approved = f"NODE27_GOVERNANCE_PGDATA_ROOT={target}\nLIMIT=7\n"
    environment.write_text(approved)
    UnitHost(units).verify_units(state, required_fences=True)
    host.release_callers_ready(state)
    environment.write_text(approved.replace("LIMIT=7", "LIMIT=99"))
    with pytest.raises(MigrationError, match="caller environment changed"):
        UnitHost(units).restore_units(state)
    assert host.fence_path(name).exists()
    environment.write_text(approved)
    state.update(stage="release_intent", writes_released=True)
    UnitHost(units).restore_units(state)
    assert not host.fence_path(name).exists()


@pytest.mark.parametrize("unsafe", ("symlink", "fifo", "device-crossing"))
def test_cluster_traversal_refuses_unsafe_interior(tmp_path: Path, monkeypatch, unsafe: str) -> None:
    tree = tmp_path / "cluster"
    tree.mkdir()
    interior = tree / "base"
    interior.mkdir()
    sentinel = _private(interior / "relation", "retained relation bytes")
    with monkeypatch.context() as patch:
        if unsafe == "symlink":
            (interior / "external").symlink_to(sentinel)
        elif unsafe == "fifo":
            os.mkfifo(interior / "external")
        else:
            real_scandir = os.scandir

            class Entries:
                def __init__(self, fd):
                    self.entries = real_scandir(fd)

                def __enter__(self):
                    for entry in self.entries:
                        info = entry.stat(follow_symlinks=False)
                        if entry.name == "relation":
                            yield SimpleNamespace(
                                name=entry.name,
                                stat=lambda **kwargs: SimpleNamespace(st_dev=info.st_dev + 1, st_mode=info.st_mode),
                            )
                        else:
                            yield entry

                def __exit__(self, *args):
                    self.entries.close()

            patch.setattr(os, "scandir", Entries)
        with pytest.raises(MigrationError):
            covered_tree(tree)
    assert sentinel.read_text() == "retained relation bytes"


def _unit_fixture(tmp_path: Path, monkeypatch, specs: dict[str, tuple[str, str]]) -> tuple[UnitHost, dict]:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    _private(runtime / "app.py", "print('owned runtime')\n")
    monkeypatch.setattr("packages.common.node27_pgdata_host.OLD_RUNTIME", str(runtime))
    units = {}
    for name, (active, kind) in specs.items():
        units[name] = {
            "LoadState": "loaded",
            "ActiveState": active,
            "SubState": "running",
            "UnitFileState": "enabled" if name.endswith(".timer") else "static",
            "Type": kind,
            "FragmentPath": str(_private(tmp_path / name, "[Unit]\nDescription=fixture\n")),
            "DropInPaths": "",
            "EnvironmentFiles": "",
            "WorkingDirectory": str(runtime) if name == DISPLAY else "",
            "ExecStart": f"{runtime}/app.py --port 55495" if name == DISPLAY else "",
        }

    class RuntimeHost(UnitHost):
        def command(self, argv, **kwargs):
            if list(argv[:3]) == ["/usr/bin/git", "-C", str(runtime)]:
                return SimpleNamespace(
                    returncode=0, stderr="", stdout="a" * 40 if argv[3] == "rev-parse" else "app.py\x00"
                )
            return super().command(argv, **kwargs)

    host = RuntimeHost(units)
    state = {
        "workspace": str(_workspace(tmp_path)),
        "operation": "92" * 16,
        "stage": "prepared",
        "writes_released": False,
        "config": {**DEFAULTS, "drain_timeout": 10},
        "display_unfenced": False,
    }
    return host, state


@pytest.mark.parametrize("active", ("activating", "deactivating"))
def test_unstable_display_is_refused_before_fencing(tmp_path: Path, monkeypatch, active: str) -> None:
    host, state = _unit_fixture(tmp_path, monkeypatch, {DISPLAY: (active, "simple")})
    with pytest.raises(MigrationError):
        state["units"] = host.units_snapshot(state["config"])
    assert host.units[DISPLAY]["ActiveState"] == active
    assert not host.fence_path(DISPLAY).exists()
    assert host.commands == []


@pytest.mark.parametrize(("active", "enabled"), (("active", "static"), ("inactive", "enabled")))
def test_cold_lane_refuses_admission(tmp_path: Path, monkeypatch, active: str, enabled: str) -> None:
    host, state = _unit_fixture(tmp_path, monkeypatch, {})
    host.units["nhms-node27-cold-residency.service"] = {
        "LoadState": "loaded",
        "ActiveState": active,
        "UnitFileState": enabled,
    }
    with pytest.raises(MigrationError):
        host.units_snapshot(state["config"])
    assert host.commands == []
    assert not (tmp_path / ".config").exists()


def test_unknown_persistent_writer_stays_fenced_without_being_killed(tmp_path: Path, monkeypatch) -> None:
    writer = "nhms-node27-download.service"
    host, state = _unit_fixture(tmp_path, monkeypatch, {writer: ("active", "simple")})
    state["units"] = host.units_snapshot(state["config"])
    # If an unsupported daemon exits during a wait, it is still not an admitted
    # oneshot. A drain timeout must not mask loss of the writer-kind refusal.
    monkeypatch.setattr(
        "packages.common.node27_pgdata_host.time.sleep", lambda _: host.units[writer].update(ActiveState="inactive")
    )
    with pytest.raises(MigrationError):
        host.install_fences(state)
    assert host.fence_path(writer).exists()
    assert host.units[writer]["ActiveState"] == "active"
    assert ("/usr/bin/systemctl", "--user", "stop", writer) not in host.commands


@pytest.mark.parametrize("initial", ("active", "activating"))
def test_display_only_lift_preserves_writers_and_drained_oneshot_is_not_replayed(
    tmp_path: Path, monkeypatch, initial: str
) -> None:
    writer, timer = "nhms-node27-download.service", "nhms-node27-download.timer"
    host, state = _unit_fixture(
        tmp_path,
        monkeypatch,
        {DISPLAY: ("active", "simple"), writer: (initial, "oneshot"), timer: ("active", "")},
    )
    state["units"] = host.units_snapshot(state["config"])
    # Model the running job completing naturally at the clock/IO boundary.
    monkeypatch.setattr(
        "packages.common.node27_pgdata_host.time.sleep", lambda _: host.units[writer].update(ActiveState="inactive")
    )
    host.install_fences(state)
    assert host.units[writer]["ActiveState"] == "inactive"
    assert ("/usr/bin/systemctl", "--user", "stop", writer) not in host.commands
    state["display_unfenced"] = True
    host.restore_units(state, display_only=True)
    assert not host.fence_path(DISPLAY).exists()
    assert host.units[DISPLAY]["ActiveState"] == "active"
    assert host.fence_path(writer).exists() and host.fence_path(timer).exists()
    assert host.units[timer]["ActiveState"] == "inactive"
    host.restore_units(state)
    assert host.units[timer]["ActiveState"] == "active"
    assert host.units[writer]["ActiveState"] == "inactive"
    assert ("/usr/bin/systemctl", "--user", "start", writer) not in host.commands


def test_nonpinned_image_refuses_before_preparation_mutation(tmp_path: Path) -> None:
    root = tmp_path / ("nhms-pgdata-oracle-" + "93" * 16)
    root.mkdir(mode=0o700)
    source, workspace = root / "source", root / "workspace"
    source.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    name = root.name + "-db"
    raw = _inspect(name=name, source=str(source), port="55494")
    raw["Image"] = raw["Config"]["Image"] = "sha256:" + "b" * 64
    host = FakeHost({name: raw})
    with pytest.raises(MigrationError):
        Migration(workspace, host=host).run(
            "prepare",
            enforce=True,
            overrides={
                "source_container": name,
                "source_pgdata": str(source),
                "target_pgdata": str(root / "target"),
                "disposable_root": str(root),
                "reserve_bytes": 1,
            },
        )
    assert not (workspace / "state.json").exists()
    assert not (root / "target").exists()
    assert raw["State"]["Running"]
    assert all(command[1] == "inspect" for command in host.commands)


def test_active_display_requires_healthy_endpoint_while_business_fences_hold(tmp_path: Path, monkeypatch) -> None:
    timer = "nhms-node27-download.timer"
    host, state = _unit_fixture(tmp_path, monkeypatch, {DISPLAY: ("active", "simple"), timer: ("active", "")})
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    state["display_unfenced"] = True
    host.restore_units(state, display_only=True)

    def unavailable(*args, **kwargs):
        raise OSError("fixture endpoint unavailable")

    monkeypatch.setattr("packages.common.node27_pgdata_host.urlopen", unavailable)
    clock = iter((0, 121))
    monkeypatch.setattr("packages.common.node27_pgdata_host.time.monotonic", lambda: next(clock))
    with pytest.raises(MigrationError):
        host.display_ready(state)
    assert host.fence_path(timer).exists()
    assert host.units[timer]["ActiveState"] == "inactive"


@pytest.mark.parametrize("initial", ("inactive", "failed"))
def test_quiescent_display_state_is_preserved_without_replay(tmp_path: Path, monkeypatch, initial: str) -> None:
    host, state = _unit_fixture(tmp_path, monkeypatch, {DISPLAY: (initial, "simple")})
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    host.restore_units(state)
    assert host.units[DISPLAY]["ActiveState"] == initial
    assert not host.fence_path(DISPLAY).exists()
    assert ("/usr/bin/systemctl", "--user", "start", DISPLAY) not in host.commands


def test_failed_timer_state_is_preserved_without_replay(tmp_path: Path, monkeypatch) -> None:
    timer = "nhms-node27-autopipe.timer"
    host, state = _unit_fixture(tmp_path, monkeypatch, {timer: ("failed", "")})
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    assert host.units[timer]["ActiveState"] == "failed"
    host.restore_units(state)
    assert host.units[timer]["ActiveState"] == "failed"
    assert not host.fence_path(timer).exists()


def _native_exec_start(command: str, *, completed: bool = False) -> str:
    result = (
        "start_time=[Fri 2026-09-11 07:23:52 CST] ; stop_time=[Fri 2026-09-11 22:54:23 CST]"
        " ; pid=1018640 ; code=exited ; status=0"
        if completed
        else "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0"
    )
    return f"{{ path=/bin/bash ; argv[]=/bin/bash -lc {command} ; ignore_errors=no ; {result} }}"


def test_native_execstart_completion_allows_owned_service_restoration(tmp_path: Path, monkeypatch) -> None:
    host, state = _unit_fixture(tmp_path, monkeypatch, {DISPLAY: ("active", "simple")})
    command = host.units[DISPLAY]["ExecStart"] + ' ; echo " ; start_time=[literal]"'
    host.units[DISPLAY]["ExecStart"] = _native_exec_start(command)
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    host.units[DISPLAY]["ExecStart"] = _native_exec_start(command, completed=True)
    host.restore_units(state)
    assert host.units[DISPLAY]["ActiveState"] == "active"
    assert not host.fence_path(DISPLAY).exists()


@pytest.mark.parametrize(
    ("old", "new"),
    (("path=/bin/bash", "path=/bin/sh"), ("--port 55495", "--port 55496"), ("ignore_errors=no", "ignore_errors=yes")),
)
def test_native_execstart_configuration_drift_keeps_service_fenced(
    tmp_path: Path, monkeypatch, old: str, new: str
) -> None:
    host, state = _unit_fixture(tmp_path, monkeypatch, {DISPLAY: ("active", "simple")})
    command = host.units[DISPLAY]["ExecStart"]
    host.units[DISPLAY]["ExecStart"] = _native_exec_start(command)
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    changed = _native_exec_start(command, completed=True)
    assert old in changed
    host.units[DISPLAY]["ExecStart"] = changed.replace(old, new, 1)
    with pytest.raises(MigrationError):
        host.restore_units(state)
    assert host.units[DISPLAY]["ActiveState"] == "inactive"
    assert host.fence_path(DISPLAY).exists()


def _primary_env_fixture(tmp_path: Path, monkeypatch):
    service = "nhms-node27-resource-governance.service"
    timer = "nhms-node27-resource-governance.timer"
    host, state = _unit_fixture(tmp_path, monkeypatch, {service: ("inactive", "oneshot"), timer: ("active", "")})
    path = _private(
        tmp_path / "primary config.env",
        f"NODE27_GOVERNANCE_PGDATA_ROOT={DEFAULTS['source_pgdata']}\nAUDIT_SETTING=original\n",
    )
    host.units[service]["Id"] = service
    host.units[service]["Environment"] = f'"NODE27_RESOURCE_GOVERNANCE_ENV_FILE={path}" NODE27_UNIT_FLAG=original'
    return host, state, service, timer, path


def test_primary_environment_allows_bound_governance_release(tmp_path: Path, monkeypatch) -> None:
    host, state, service, timer, path = _primary_env_fixture(tmp_path, monkeypatch)
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    path.write_text(path.read_text().replace(DEFAULTS["source_pgdata"], DEFAULTS["target_pgdata"]))
    host.units[service]["Environment"] = f'NODE27_UNIT_FLAG=original "NODE27_RESOURCE_GOVERNANCE_ENV_FILE={path}"'
    state["stage"] = "release_intent"
    state["writes_released"] = True
    host.restore_units(state)
    assert host.units[timer]["ActiveState"] == "active"
    assert not host.fence_path(timer).exists()
    assert not host.fence_path(service).exists()


@pytest.mark.parametrize("drift", ("file-bytes", "binding", "unit-environment"))
def test_primary_environment_drift_keeps_callers_fenced(tmp_path: Path, monkeypatch, drift: str) -> None:
    host, state, service, timer, path = _primary_env_fixture(tmp_path, monkeypatch)
    state["units"] = host.units_snapshot(state["config"])
    host.install_fences(state)
    if drift == "file-bytes":
        path.write_text(path.read_text().replace("AUDIT_SETTING=original", "AUDIT_SETTING=changed"))
    elif drift == "binding":
        replacement = _private(tmp_path / "replacement.env", path.read_text())
        host.units[service]["Environment"] = host.units[service]["Environment"].replace(str(path), str(replacement))
    else:
        host.units[service]["Environment"] = host.units[service]["Environment"].replace(
            "NODE27_UNIT_FLAG=original", "NODE27_UNIT_FLAG=changed"
        )
    with pytest.raises(MigrationError):
        host.restore_units(state)
    assert host.units[timer]["ActiveState"] == "inactive"
    assert host.fence_path(timer).exists()
    assert host.fence_path(service).exists()


def test_primary_environment_symlink_refuses_before_fencing(tmp_path: Path, monkeypatch) -> None:
    host, state, service, timer, path = _primary_env_fixture(tmp_path, monkeypatch)
    alias = tmp_path / "alias.env"
    alias.symlink_to(path)
    host.units[service]["Environment"] = host.units[service]["Environment"].replace(str(path), str(alias))
    with pytest.raises(SafeFilesystemError):
        host.units_snapshot(state["config"])
    assert host.units[timer]["ActiveState"] == "active"
    assert not host.fence_path(timer).exists()
    assert not host.fence_path(service).exists()


@pytest.mark.parametrize("invalid", ("missing-source", "escaped-environment", "duplicate-assignment", "wrong-source"))
def test_primary_environment_unproved_input_refuses_before_fencing(tmp_path: Path, monkeypatch, invalid: str) -> None:
    host, state, service, timer, path = _primary_env_fixture(tmp_path, monkeypatch)
    if invalid == "missing-source":
        path.write_text("AUDIT_SETTING=original\n")
    elif invalid == "escaped-environment":
        host.units[service]["Environment"] += r' EXTRA="line\nvalue"'
    elif invalid == "duplicate-assignment":
        host.units[service]["Environment"] += " NODE27_UNIT_FLAG=changed"
    else:
        path.write_text(path.read_text().replace(DEFAULTS["source_pgdata"], DEFAULTS["target_pgdata"]))
    with pytest.raises(MigrationError):
        host.units_snapshot(state["config"])
    assert host.units[timer]["ActiveState"] == "active"
    assert not host.fence_path(timer).exists()
    assert not host.fence_path(service).exists()
