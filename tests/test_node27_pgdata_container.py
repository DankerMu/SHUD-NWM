"""Behavioral contracts for exact PGDATA container snapshots and serialization."""

from __future__ import annotations

import json

import pytest

from packages.common.node27_pgdata_container import (
    PINNED_IMAGE_ID,
    ContainerContractError,
    normalize_raw_inspect,
    serialize_container_argv,
)


def _inspect(*, env: list[str] | None = None, include_unsupported: bool = False) -> dict:
    payload = {
        "Id": "sha256:container-before",
        "Name": "/nhms-db",
        "Image": PINNED_IMAGE_ID,
        "Config": {
            "Image": PINNED_IMAGE_ID,
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
    assert snapshot.image == PINNED_IMAGE_ID
    assert snapshot.resolved_image_id == PINNED_IMAGE_ID
    assert public["resolved_image_id"] == PINNED_IMAGE_ID
    assert private["resolved_image_id"] == PINNED_IMAGE_ID
    assert "ultra-secret" not in json.dumps(public)


def test_known_docker_defaults_are_inert_but_custom_nondefault_fields_still_block_recreation() -> None:
    raw = _inspect()
    raw["Config"].update(
        {"Hostname": "a" * 12, "ExposedPorts": {"5432/tcp": {}}, "AttachStdout": True, "AttachStderr": True}
    )
    raw["HostConfig"].update(
        {
            "CgroupnsMode": "private",
            "IpcMode": "private",
            "Runtime": "runc",
            "ConsoleSize": [0, 0],
            "LogConfig": {"Type": "json-file", "Config": {}},
        }
    )
    assert normalize_raw_inspect(raw).config_digest == normalize_raw_inspect(_inspect()).config_digest

    with pytest.raises(ContainerContractError, match="unsupported"):
        normalize_raw_inspect(_inspect(include_unsupported=True))
    interactive = _inspect()
    interactive["Config"]["AttachStdin"] = True
    with pytest.raises(ContainerContractError, match="unsupported"):
        normalize_raw_inspect(interactive)


def test_malformed_or_oversized_or_nonobject_raw_inspect_is_rejected_as_inert_data() -> None:
    with pytest.raises(ContainerContractError, match="object"):
        normalize_raw_inspect([])
    with pytest.raises(ContainerContractError, match="Id"):
        normalize_raw_inspect({"Config": {"Image": "x", "Env": "source this"}, "HostConfig": {}, "Mounts": []})
    with pytest.raises(ContainerContractError, match="byte ceiling"):
        normalize_raw_inspect(_inspect(), max_bytes=8)


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

    with pytest.raises(ContainerContractError, match="unsupported non-default|MaskedPaths"):
        normalize_raw_inspect(raw)


_CAPTURED_MASKED_PATHS = [
    "/proc/asound",
    "/proc/acpi",
    "/proc/interrupts",
    "/proc/kcore",
    "/proc/keys",
    "/proc/latency_stats",
    "/proc/timer_list",
    "/proc/timer_stats",
    "/proc/sched_debug",
    "/proc/scsi",
    "/sys/firmware",
    "/sys/devices/virtual/powercap",
    "/sys/devices/system/cpu/cpu0/thermal_throttle",
    "/sys/devices/system/cpu/cpu1/thermal_throttle",
    "/sys/devices/system/cpu/cpu2/thermal_throttle",
    "/sys/devices/system/cpu/cpu3/thermal_throttle",
]
_CAPTURED_READONLY_PATHS = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"]
_SYNTHETIC_MEMORY = 536_870_912
_SYNTHETIC_MEMORY_SWAP = 1_073_741_824


def _captured_host_defaults(*, memory: int = _SYNTHETIC_MEMORY, memory_swap: int = _SYNTHETIC_MEMORY_SWAP) -> dict:
    raw = _inspect()
    raw["HostConfig"]["Memory"] = memory
    raw["HostConfig"]["MemorySwap"] = memory_swap
    raw["HostConfig"]["MaskedPaths"] = list(_CAPTURED_MASKED_PATHS)
    raw["HostConfig"]["ReadonlyPaths"] = list(_CAPTURED_READONLY_PATHS)
    return raw


@pytest.mark.parametrize(
    "masked",
    (
        ["/custom/masked-path"],
        [path for path in _CAPTURED_MASKED_PATHS if path != "/proc/interrupts"],
        [
            *(_CAPTURED_MASKED_PATHS[:12]),
            "/sys/devices/system/cpu/cpu0/thermal_throttle",
            "/sys/devices/system/cpu/cpu2/thermal_throttle",
        ],
    ),
)
def test_nondefault_or_malformed_masked_paths_are_refused(masked: list[str]) -> None:
    raw = _captured_host_defaults()
    raw["HostConfig"]["MaskedPaths"] = masked

    with pytest.raises(ContainerContractError, match="MaskedPaths"):
        normalize_raw_inspect(raw)


def test_pure_serializer_does_not_add_cold_bind_and_can_hide_environment() -> None:
    snapshot = normalize_raw_inspect(_inspect())
    argv = serialize_container_argv(snapshot, name="owned-db", environment_file="/private/env", create_only=True)
    assert argv[:4] == ("/usr/bin/docker", "create", "--name", "owned-db")
    assert all("nhms_cold" not in value for value in argv)
    assert "POSTGRES_PASSWORD=ultra-secret" not in argv
    assert ("--env-file", "/private/env") == tuple(argv[argv.index("--env-file") : argv.index("--env-file") + 2])


def test_nondefault_healthcheck_or_multi_argument_entrypoint_blocks_exact_recreation() -> None:
    healthcheck = _inspect()
    healthcheck["Config"]["Healthcheck"] = {"Test": ["CMD-SHELL", "pg_isready -U nhms"]}
    with pytest.raises(ContainerContractError, match="Healthcheck"):
        serialize_container_argv(normalize_raw_inspect(healthcheck), name="owned-db")

    entrypoint = _inspect()
    entrypoint["Config"]["Entrypoint"] = ["bash", "-c"]
    with pytest.raises(ContainerContractError, match="Entrypoint"):
        serialize_container_argv(normalize_raw_inspect(entrypoint), name="owned-db")


def test_captured_docker_28_hostconfig_defaults_are_reconstructed_without_secrets() -> None:
    snapshot = normalize_raw_inspect(_captured_host_defaults())
    argv = serialize_container_argv(snapshot, name="owned-db")
    public = json.dumps(snapshot.public_payload())
    private = json.dumps(snapshot.private_payload())

    assert snapshot.memory == _SYNTHETIC_MEMORY
    assert snapshot.memory_swap == _SYNTHETIC_MEMORY_SWAP
    assert snapshot.masked_paths == tuple(_CAPTURED_MASKED_PATHS)
    assert snapshot.readonly_paths == tuple(_CAPTURED_READONLY_PATHS)
    assert ("--memory", str(_SYNTHETIC_MEMORY)) == tuple(argv[argv.index("--memory") : argv.index("--memory") + 2])
    assert ("--memory-swap", str(_SYNTHETIC_MEMORY_SWAP)) == tuple(
        argv[argv.index("--memory-swap") : argv.index("--memory-swap") + 2]
    )
    assert "--masked-path" not in argv and "--read-only-path" not in argv
    assert "ultra-secret" not in public
    assert "ultra-secret" in private
    assert snapshot.public_payload()["memory_swap"] == _SYNTHETIC_MEMORY_SWAP
    assert snapshot.public_payload()["masked_paths"] == list(_CAPTURED_MASKED_PATHS)
    assert snapshot.private_payload()["readonly_paths"] == list(_CAPTURED_READONLY_PATHS)


def test_zero_memory_and_swap_emit_neither_memory_flag() -> None:
    snapshot = normalize_raw_inspect(_captured_host_defaults(memory=0, memory_swap=0))
    argv = serialize_container_argv(snapshot, name="owned-db")

    assert snapshot.memory == 0
    assert snapshot.memory_swap == 0
    assert "--memory" not in argv
    assert "--memory-swap" not in argv


@pytest.mark.parametrize(
    ("memory", "memory_swap", "match"),
    (
        (0, _SYNTHETIC_MEMORY_SWAP, "MemorySwap"),
        (_SYNTHETIC_MEMORY, -1, "MemorySwap"),
        (_SYNTHETIC_MEMORY, _SYNTHETIC_MEMORY - 1, "MemorySwap"),
    ),
)
def test_inconsistent_or_negative_memory_swap_is_refused(memory: int, memory_swap: int, match: str) -> None:
    raw = _captured_host_defaults(memory=memory, memory_swap=memory_swap)

    with pytest.raises(ContainerContractError, match=match):
        snapshot = normalize_raw_inspect(raw)
        serialize_container_argv(snapshot, name="owned-db")


def test_unsupported_bind_option_and_duplicate_or_empty_binds_are_refused() -> None:
    raw = _inspect()
    raw["HostConfig"]["Binds"] = ["/home/nwm/nhms-pgdata:/home/postgres/pgdata/data:rw,Z"]
    with pytest.raises(ContainerContractError, match="bind"):
        normalize_raw_inspect(raw)

    raw = _inspect()
    raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data:rw",
    ]
    with pytest.raises(ContainerContractError, match="bind"):
        normalize_raw_inspect(raw)


def test_config_stop_timeout_is_modeled_and_conflicting_hostconfig_is_refused() -> None:
    raw = _inspect()
    raw["Config"]["StopTimeout"] = 300
    raw["HostConfig"].pop("StopTimeout", None)
    snapshot = normalize_raw_inspect(raw)
    argv = serialize_container_argv(snapshot, name="owned-db")
    assert snapshot.stop_timeout == 300
    assert ("--stop-timeout", "300") == tuple(argv[argv.index("--stop-timeout") : argv.index("--stop-timeout") + 2])

    conflict = _inspect()
    conflict["Config"]["StopTimeout"] = 300
    conflict["HostConfig"]["StopTimeout"] = 10
    with pytest.raises(ContainerContractError, match="StopTimeout"):
        normalize_raw_inspect(conflict)


def test_non_sha_document_image_is_refused() -> None:
    raw = _inspect()
    raw["Image"] = "timescale/timescaledb-ha:pg15-latest"
    with pytest.raises(ContainerContractError, match="sha256"):
        normalize_raw_inspect(raw)
