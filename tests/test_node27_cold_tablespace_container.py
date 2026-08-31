"""Contract tests for inert exact nhms-db snapshot/recreate/rollback planning."""

from __future__ import annotations

import json

import pytest

from packages.common.node27_cold_tablespace_container import (
    COLD_BIND,
    COLD_CONTAINER_PATH,
    COLD_HOST_PATH,
    ContainerContractError,
    build_recreate_argv,
    diff_container_config,
    normalize_raw_inspect,
    rollback_plan,
)


def _inspect(*, env: list[str] | None = None, include_unsupported: bool = False) -> dict:
    payload = {
        "Id": "sha256:container-before",
        "Name": "/nhms-db",
        "Config": {
            "Image": "timescale/timescaledb-ha:pg15-latest",
            "Env": env or ["POSTGRES_PASSWORD=ultra-secret", "POSTGRES_USER=nhms", "PGDATA=/home/postgres/pgdata/data"],
            "Cmd": ["postgres"],
            "Entrypoint": None,
            "WorkingDir": "/",
            "User": "1005:1005",
            "Labels": {"org.nhms.role": "primary"},
            "StopSignal": "SIGINT",
            "Healthcheck": None,
        },
        "HostConfig": {
            "Binds": [
                "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data:rw",
                "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence:rw",
            ],
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NanoCpus": 2_000_000_000,
            "Memory": 8_589_934_592,
            "ShmSize": 1_073_741_824,
            "StopTimeout": 300,
            "ReadonlyRootfs": False,
            "CapAdd": [],
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": "bridge",
            "Privileged": False,
            "PublishAllPorts": False,
            "AutoRemove": False,
            "VolumesFrom": [],
            "Devices": [],
            "DeviceRequests": [],
            "Tmpfs": {},
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/home/nwm/nhms-pgdata",
                "Destination": "/home/postgres/pgdata/data",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/home/nwm/nhms-evidence",
                "Destination": "/var/lib/postgresql/evidence",
                "RW": True,
            },
        ],
    }
    if include_unsupported:
        payload["HostConfig"]["Links"] = ["other:db"]
    return payload


def test_recreate_argv_preserves_exact_supported_nondefault_configuration_and_adds_one_bind() -> None:
    before = normalize_raw_inspect(_inspect())

    argv = build_recreate_argv(before, replacement_name="nhms-db")

    assert argv[:5] == ("/usr/bin/docker", "run", "-d", "--name", "nhms-db")
    assert "POSTGRES_PASSWORD=ultra-secret" in argv
    assert ("-p", "127.0.0.1:55432:5432") == tuple(argv[argv.index("-p") : argv.index("-p") + 2])
    assert ("--memory", "8589934592") == tuple(argv[argv.index("--memory") : argv.index("--memory") + 2])
    assert ("--cpus", "2") == tuple(argv[argv.index("--cpus") : argv.index("--cpus") + 2])
    assert ("--restart", "unless-stopped") == tuple(argv[argv.index("--restart") : argv.index("--restart") + 2])
    assert COLD_BIND in argv
    assert argv[-2:] == ("timescale/timescaledb-ha:pg15-latest", "postgres")
    assert not any("/bin/sh" in item or "$(" in item for item in argv)


def test_normalized_public_snapshot_excludes_secret_values_but_private_snapshot_keeps_reconstructible_env() -> None:
    snapshot = normalize_raw_inspect(_inspect())

    public = snapshot.public_payload()
    private = snapshot.private_payload()

    rendered_public = json.dumps(public)
    rendered_private = json.dumps(private)
    assert "ultra-secret" not in rendered_public
    assert public["environment_names"] == ["PGDATA", "POSTGRES_PASSWORD", "POSTGRES_USER"]
    assert "ultra-secret" in rendered_private
    assert public["config_digest"] == snapshot.config_digest


def test_exact_diff_accepts_only_cold_bind_and_rejects_image_env_port_resource_or_mount_drift() -> None:
    before = normalize_raw_inspect(_inspect())
    recreated = _inspect()
    recreated["Id"] = "sha256:container-after"
    recreated["HostConfig"]["Binds"].append(COLD_BIND)
    after = normalize_raw_inspect(recreated)

    assert before.container_id != after.container_id
    assert before.with_cold_bind().config_digest == after.config_digest
    assert diff_container_config(before, after).approved is True

    changed = _inspect()
    changed["Config"]["Image"] = "other:image"
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect(env=["POSTGRES_PASSWORD=changed", "POSTGRES_USER=nhms", "PGDATA=/home/postgres/pgdata/data"])
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostPort"] = "55433"
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["Memory"] = 1
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False

    changed = _inspect()
    changed["HostConfig"]["Binds"].append("/tmp/extra:/extra:rw")
    assert diff_container_config(before, normalize_raw_inspect(changed)).approved is False


def test_nondefault_healthcheck_or_multi_argument_entrypoint_blocks_exact_recreation() -> None:
    healthcheck = _inspect()
    healthcheck["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "pg_isready -U nhms"]}
    with pytest.raises(ContainerContractError, match="Healthcheck"):
        build_recreate_argv(normalize_raw_inspect(healthcheck), replacement_name="nhms-db")

    entrypoint = _inspect()
    entrypoint["Config"]["Entrypoint"] = ["bash", "-c"]
    with pytest.raises(ContainerContractError, match="Entrypoint"):
        build_recreate_argv(normalize_raw_inspect(entrypoint), replacement_name="nhms-db")


def test_known_docker_defaults_are_inert_but_custom_nondefault_fields_still_block_recreation() -> None:
    raw = _inspect()
    raw["Config"].update({"Hostname": "a" * 12, "ExposedPorts": {"5432/tcp": {}}})
    raw["HostConfig"].update(
        {
            "CgroupnsMode": "private",
            "IpcMode": "private",
            "Runtime": "runc",
            "ConsoleSize": [0, 0],
            "LogConfig": {"Type": "json-file", "Config": {}},
        }
    )
    assert normalize_raw_inspect(raw).name == "nhms-db"

    with pytest.raises(ContainerContractError, match="unsupported"):
        normalize_raw_inspect(_inspect(include_unsupported=True))


def test_malformed_or_oversized_or_nonobject_raw_inspect_is_rejected_as_inert_data() -> None:
    with pytest.raises(ContainerContractError, match="object"):
        normalize_raw_inspect([])
    with pytest.raises(ContainerContractError, match="Id"):
        normalize_raw_inspect({"Config": {"Image": "x", "Env": "source this"}, "HostConfig": {}, "Mounts": []})
    with pytest.raises(ContainerContractError, match="byte ceiling"):
        normalize_raw_inspect(_inspect(), max_bytes=8)


def test_rollback_plan_never_deletes_host_path_if_any_reference_or_identity_doubt_remains() -> None:
    safe = rollback_plan(
        installer_container="nhms-db",
        prior_container="nhms-db-before",
        installer_created_catalog=True,
        catalog_dependents=0,
        pg_tblspc_references=(),
        current_bind_references=(),
        stopped_bind_references=(),
        host_path_identity_matches=True,
        host_path_empty=True,
    )
    assert safe.restore_prior is True
    assert safe.remove_host_path is True

    for kwargs in (
        {"catalog_dependents": 1},
        {"pg_tblspc_references": (COLD_CONTAINER_PATH,)},
        {"current_bind_references": (COLD_BIND,)},
        {"stopped_bind_references": (COLD_BIND,)},
        {"host_path_identity_matches": False},
        {"host_path_empty": False},
    ):
        arguments = {
            "installer_container": "nhms-db",
            "prior_container": "nhms-db-before",
            "installer_created_catalog": True,
            "catalog_dependents": 0,
            "pg_tblspc_references": (),
            "current_bind_references": (),
            "stopped_bind_references": (),
            "host_path_identity_matches": True,
            "host_path_empty": True,
        }
        arguments.update(kwargs)
        plan = rollback_plan(**arguments)
        assert plan.remove_host_path is False, kwargs
        assert plan.blockers


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PidsLimit", 42),
        ("LogConfig", {"Type": "syslog", "Config": {"tag": "nhms"}}),
        ("Ulimits", [{"Name": "nofile", "Soft": 1024, "Hard": 2048}]),
        ("MaskedPaths", ["/custom/masked-path"]),
    ],
)
def test_unreconstructible_nondefault_host_configuration_blocks_before_replacement(field: str, value: object) -> None:
    raw = _inspect()
    raw["HostConfig"][field] = value

    with pytest.raises(ContainerContractError, match="unsupported non-default"):
        normalize_raw_inspect(raw)


def test_rollback_plan_does_not_remove_an_uncreated_installer_container() -> None:
    plan = rollback_plan(
        installer_container="nhms-db",
        prior_container="nhms-db-before",
        installer_created_catalog=False,
        catalog_dependents=0,
        pg_tblspc_references=(),
        current_bind_references=(),
        stopped_bind_references=(),
        host_path_identity_matches=True,
        host_path_empty=True,
        installer_container_created=False,
    )

    assert plan.remove_installer_container is False


def test_cold_bind_is_exact_fixed_host_to_container_mapping() -> None:
    assert COLD_BIND == f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw"
