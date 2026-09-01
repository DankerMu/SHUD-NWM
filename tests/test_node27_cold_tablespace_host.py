"""Host boundary tests using synthetic paths and fake Docker execution only."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.node27_cold_tablespace_evidence import EvidencePolicy
from packages.common.node27_cold_tablespace_host import (
    ColdHostError,
    DockerBoundary,
    EvidencePaths,
    SystemdBoundary,
    inspect_host_path,
    inspect_running_target,
    inspect_storage_evidence,
)


def test_host_path_inspection_refuses_any_path_other_than_fixed_production_contract(tmp_path: Path) -> None:
    with pytest.raises(ColdHostError, match="identity contract"):
        inspect_host_path(tmp_path)


def test_docker_boundary_rejects_nontrusted_binary() -> None:
    with pytest.raises(ColdHostError, match="trusted"):
        DockerBoundary(docker_bin="/tmp/docker")


def test_docker_boundary_parses_one_bounded_inert_inspect_document(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"Id": "sha256:test", "Name": "/nhms-db", "Config": {}, "HostConfig": {}, "Mounts": []}]

    def fake(argv, **_kwargs):
        assert argv == ("/usr/bin/docker", "inspect", "nhms-db")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("packages.common.node27_cold_tablespace_host.run_bounded_command", fake)

    assert DockerBoundary().inspect("nhms-db")["Id"] == "sha256:test"


def test_docker_boundary_refuses_malformed_or_failed_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "packages.common.node27_cold_tablespace_host.run_bounded_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    with pytest.raises(ColdHostError, match="malformed"):
        DockerBoundary().inspect("nhms-db")

    monkeypatch.setattr(
        "packages.common.node27_cold_tablespace_host.run_bounded_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="password=secret"),
    )
    with pytest.raises(ColdHostError, match="failed"):
        DockerBoundary().inspect("nhms-db")


def test_descriptor_bound_storage_boundary_carries_parsed_health_and_catalog_bound_backup_scope(tmp_path: Path) -> None:
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    policy = EvidencePolicy(
        expected_hostname="node27-test",
        array_device="/dev/md0",
        max_age_seconds=300,
        expected_uid=os.getuid(),
        approved_modes=(0o600,),
        mdadm_argv=("/usr/sbin/mdadm", "--detail", "/dev/md0"),
        smartctl_prefix=("/usr/sbin/smartctl",),
        backup_argv=("/usr/local/sbin/nhms-backup-inventory", "--json"),
        expected_pgdata="/home/nwm/nhms-pgdata",
    )

    def write(name: str, payload: dict) -> Path:
        target = tmp_path / name
        target.write_text(json.dumps(payload), encoding="utf-8")
        target.chmod(0o600)
        return target

    def envelope(command: list[str], subject: dict, output: str) -> dict:
        return {
            "schema_version": "1.0",
            "captured_at": "2026-08-31T12:00:00Z",
            "hostname": "node27-test",
            "command": {"argv": command},
            "subject": subject,
            "output": output,
        }

    mdadm_output = "\n".join(
        (
            "/dev/md0:",
            "Raid Devices : 2",
            "Active Devices : 2",
            "Working Devices : 2",
            "Failed Devices : 0",
            "Spare Devices : 0",
            "State : clean",
            "",
            " 0 8 17 0 active sync /dev/sdb1",
            " 1 8 33 1 active sync /dev/sdc1",
        )
    )
    mdadm = write(
        "mdadm.json",
        envelope(
            ["/usr/sbin/mdadm", "--detail", "/dev/md0"],
            {"array_device": "/dev/md0"},
            mdadm_output,
        ),
    )
    smart_output = "SMART overall-health test result: PASSED"
    smart = {
        device: write(
            f"{Path(device).name}.json",
            envelope(["/usr/sbin/smartctl", "-H", device], {"device": device}, smart_output),
        )
        for device in ("/dev/sdb1", "/dev/sdc1")
    }
    target = "/home/postgres/pgdata/tablespaces/nhms_cold"
    backup_payload = envelope(
        ["/usr/local/sbin/nhms-backup-inventory", "--json"],
        {"pgdata": "/home/nwm/nhms-pgdata", "external_pg_tblspc_targets": [target]},
        "inventory complete",
    )
    backup_payload["covered_paths"] = ["/home/nwm/nhms-pgdata", target]
    health, backup = inspect_storage_evidence(
        EvidencePaths(mdadm=mdadm, smart=smart, backup=write("backup.json", backup_payload)),
        policy=policy,
        external_targets=(target,),
        now=now,
    )

    assert health["healthy"] is True
    assert [item["device"] for item in health["smart"]] == ["/dev/sdb1", "/dev/sdc1"]
    assert backup["complete"] is True
    assert backup["missing_targets"] == []


def test_systemd_boundary_requires_all_writer_timer_units_to_be_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **_kwargs: object):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ActiveState=inactive\nSubState=dead\nResult=success\n", stderr="")

    boundary = SystemdBoundary(runner=runner)
    observed = boundary.inspect_quiescence(("unit-a.service", "unit-b.timer"))

    assert observed["units"]["unit-a.service"]["active_state"] == "inactive"
    assert all(call[:4] == ("/usr/bin/systemctl", "--user", "--no-pager", "show") for call in calls)


def test_docker_action_uses_direct_argv_and_does_not_expose_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    kwargs_seen: list[dict] = []

    def fake(argv, **kwargs):
        seen.append(list(argv))
        kwargs_seen.append(kwargs)
        if argv[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:test",
                            "Name": "/nhms-db",
                            "Config": {},
                            "HostConfig": {},
                            "Mounts": [],
                        }
                    ]
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="POSTGRES_PASSWORD=secret")

    monkeypatch.setattr("packages.common.node27_cold_tablespace_host.run_bounded_command", fake)
    result = DockerBoundary().action(("/usr/bin/docker", "stop", "nhms-db"))

    assert result == {"returncode": 0}
    assert ["/usr/bin/docker", "stop", "nhms-db"] in seen
    stop_timeouts = [item.get("timeout", 5) for item, argv in zip(kwargs_seen, seen) if argv[1] == "stop"]
    assert stop_timeouts and stop_timeouts[0] >= 90


def test_docker_stop_uses_observed_stop_timeout_plus_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    def fake(argv, **kwargs):
        seen.append({"argv": list(argv), **kwargs})
        if argv[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:test",
                            "Name": "/nhms-db",
                            "Config": {"StopTimeout": 300, "Image": "timescale/timescaledb-ha:pg15-latest"},
                            "HostConfig": {},
                            "Image": "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
                            "Mounts": [],
                        }
                    ]
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("packages.common.node27_cold_tablespace_host.run_bounded_command", fake)
    docker = DockerBoundary()
    docker.action(("/usr/bin/docker", "stop", "nhms-db"))
    stop = next(item for item in seen if item["argv"][1] == "stop")
    inspect = next(item for item in seen if item["argv"][1] == "inspect")
    assert inspect.get("timeout", 5) == 5
    assert stop["timeout"] >= 390


def test_docker_action_timeout_after_possible_mutation_retains_pending_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.common.compressed_chunk_cold_runtime_catalog import ColdRuntimeError

    def fake(argv, **kwargs):
        if argv[1] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "sha256:test",
                            "Name": "/nhms-db",
                            "Config": {"StopTimeout": 10},
                            "HostConfig": {},
                            "Mounts": [],
                        }
                    ]
                ),
                stderr="",
            )
        raise ColdRuntimeError("target inspector timed out", error_class="target_identity", stage="target_identity")

    monkeypatch.setattr("packages.common.node27_cold_tablespace_host.run_bounded_command", fake)
    with pytest.raises(ColdHostError, match="timed out"):
        DockerBoundary().action(("/usr/bin/docker", "stop", "nhms-db"))


def test_docker_stop_refuses_when_stop_timeout_cannot_be_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv, **kwargs):
        seen.append(list(argv))
        if argv[1] == "inspect":
            return SimpleNamespace(returncode=1, stdout="", stderr="no such container")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("packages.common.node27_cold_tablespace_host.run_bounded_command", fake)
    with pytest.raises(ColdHostError, match="StopTimeout|inspect|failed"):
        DockerBoundary().action(("/usr/bin/docker", "stop", "nhms-db"))
    assert not any(item[1] == "stop" for item in seen)


def test_inspect_running_target_refuses_uid_only_config_user(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    docker = _running_target_docker(monkeypatch, seen=seen, config_user="1005")
    with pytest.raises(ColdHostError, match="User"):
        inspect_running_target(docker, expected_uid=1005, expected_gid=1005)
    assert seen == []


def test_inspect_running_target_refuses_config_user_mismatch_before_writable_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    docker = _running_target_docker(monkeypatch, seen=seen)
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda name: {
            "Config": {"User": "postgres"},
            "Mounts": [
                {
                    "Source": str(docker.identity.host_path),
                    "Destination": docker.identity.container_path,
                }
            ],
        },
    )

    with pytest.raises(ColdHostError, match="User"):
        inspect_running_target(docker, expected_uid=1005, expected_gid=1005)

    assert seen == []


def _running_target_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host_uid: int = 1005,
    host_gid: int = 1005,
    config_user: str | None = None,
    seen: list[list[str]] | None = None,
) -> DockerBoundary:
    docker = DockerBoundary()
    identity = docker.identity
    monkeypatch.setattr(
        "packages.common.node27_cold_tablespace_host.inspect_host_path",
        lambda **kwargs: {
            "exists": True,
            "is_symlink": False,
            "is_directory": True,
            "entry_count": 0,
            "uid": host_uid,
            "gid": host_gid,
            "mode": 0o700,
            "mount_device": "8:11",
            "device_identity": "8:11:1",
            "free_bytes": 1_000_000,
        },
    )
    monkeypatch.setattr(
        docker,
        "inspect",
        lambda name: {
            "Config": {"User": config_user if config_user is not None else f"{host_uid}:{host_gid}"},
            "Mounts": [
                {
                    "Source": str(identity.host_path),
                    "Destination": identity.container_path,
                }
            ]
        },
    )
    if seen is not None:
        monkeypatch.setattr(docker, "action", lambda argv: seen.append(list(argv)) or {"returncode": 0})
    return docker


def test_inspect_running_target_uses_expected_numeric_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    docker = _running_target_docker(monkeypatch, seen=seen)

    observed = inspect_running_target(docker, expected_uid=1005, expected_gid=1005)

    assert observed["writable"] is True
    assert observed["host_uid"] == 1005
    assert observed["host_gid"] == 1005
    exec_argv = seen[0]
    assert exec_argv[exec_argv.index("--user") + 1] == "1005:1005"
    assert "postgres" not in exec_argv


def test_inspect_running_target_refuses_host_owner_mismatch_before_docker_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    docker = _running_target_docker(
        monkeypatch, host_uid=999, host_gid=999, config_user="1005:1005", seen=seen
    )

    with pytest.raises(ColdHostError, match="owner"):
        inspect_running_target(docker, expected_uid=1005, expected_gid=1005)

    assert seen == []


def test_inspect_running_target_refuses_ambiguous_or_negative_expected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _running_target_docker(monkeypatch)

    for bad_uid, bad_gid in ((True, 1005), (1005, True), (-1, 1005), (1005, -1)):
        with pytest.raises(ColdHostError, match="non-negative"):
            inspect_running_target(docker, expected_uid=bad_uid, expected_gid=bad_gid)
