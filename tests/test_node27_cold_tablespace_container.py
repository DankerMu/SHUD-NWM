"""Contract tests for inert exact nhms-db snapshot/recreate/rollback planning."""

from __future__ import annotations

import json

import pytest

from packages.common.compressed_chunk_cold_residency import PINNED_IMAGE_ID, PINNED_IMAGE_REF
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
    assert argv[-2:] == (PINNED_IMAGE_ID, "postgres")
    assert PINNED_IMAGE_REF not in argv
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
    assert snapshot.image == PINNED_IMAGE_ID
    assert snapshot.resolved_image_id == PINNED_IMAGE_ID
    assert public["resolved_image_id"] == PINNED_IMAGE_ID
    assert private["resolved_image_id"] == PINNED_IMAGE_ID
    assert "ultra-secret" not in json.dumps(public)


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

    same_ref_different_id = _inspect()
    same_ref_different_id["Image"] = "sha256:" + "0" * 64
    same_ref_different_id["HostConfig"]["Binds"].append(COLD_BIND)
    drifted = normalize_raw_inspect(same_ref_different_id)
    assert drifted.image == PINNED_IMAGE_ID
    assert drifted.resolved_image_id != before.resolved_image_id
    assert diff_container_config(before, drifted).approved is False


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

    with pytest.raises(ContainerContractError, match="unsupported non-default|MaskedPaths"):
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


def test_captured_docker_28_hostconfig_defaults_are_reconstructed_without_secrets() -> None:
    snapshot = normalize_raw_inspect(_captured_host_defaults())
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
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
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")

    assert snapshot.memory == 0
    assert snapshot.memory_swap == 0
    assert "--memory" not in argv
    assert "--memory-swap" not in argv


def test_altered_memory_swap_or_derived_paths_fail_exact_diff() -> None:
    before = normalize_raw_inspect(_captured_host_defaults())
    after_raw = _captured_host_defaults()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"].append(COLD_BIND)
    after = normalize_raw_inspect(after_raw)
    assert diff_container_config(before, after).approved is True

    swapped = _captured_host_defaults()
    swapped["HostConfig"]["MemorySwap"] = _SYNTHETIC_MEMORY_SWAP + 1
    assert diff_container_config(before, normalize_raw_inspect(swapped)).approved is False

    missing_paths = _captured_host_defaults()
    missing_paths["HostConfig"].pop("MaskedPaths")
    missing_paths["HostConfig"].pop("ReadonlyPaths")
    assert diff_container_config(before, normalize_raw_inspect(missing_paths)).approved is False

    readonly_changed = _captured_host_defaults()
    readonly_changed["HostConfig"]["ReadonlyPaths"] = ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys"]
    with pytest.raises(ContainerContractError, match="ReadonlyPaths"):
        normalize_raw_inspect(readonly_changed)


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
        build_recreate_argv(snapshot, replacement_name="nhms-db")


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


def test_cold_bind_is_exact_fixed_host_to_container_mapping() -> None:
    assert COLD_BIND == f"{COLD_HOST_PATH}:{COLD_CONTAINER_PATH}:rw"


def test_real_two_segment_default_rw_binds_normalize_recreate_and_diff() -> None:
    raw = _inspect()
    raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm/Basins:/data/GHDC:rw",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
    ]
    snapshot = normalize_raw_inspect(raw)
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
    volumes = [argv[index + 1] for index, item in enumerate(argv) if item in {"-v", "--volume"}]

    assert "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data:rw" in snapshot.binds
    assert "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence:rw" in snapshot.binds
    assert "/home/ghdc/nwm/Basins:/data/GHDC:rw" in snapshot.binds
    assert all(":" in bind for bind in volumes)
    after_raw = _inspect()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm/Basins:/data/GHDC:rw",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
        COLD_BIND,
    ]
    after = normalize_raw_inspect(after_raw)
    assert diff_container_config(snapshot, after).approved is True
    assert after.binds != snapshot.binds
    assert set(after.binds) - set(snapshot.binds) == {COLD_BIND}


def test_live_three_two_segment_binds_only_differ_by_cold_bind() -> None:
    raw = _inspect()
    raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm:/data/GHDC",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
    ]
    before = normalize_raw_inspect(raw)
    after_raw = _inspect()
    after_raw["Id"] = "sha256:container-after"
    after_raw["HostConfig"]["Binds"] = [
        "/home/nwm/nhms-pgdata:/home/postgres/pgdata/data",
        "/home/ghdc/nwm:/data/GHDC",
        "/home/nwm/nhms-evidence:/var/lib/postgresql/evidence",
        COLD_BIND,
    ]
    after = normalize_raw_inspect(after_raw)
    diff = diff_container_config(before, after)
    assert diff.approved is True
    assert diff.changed_fields == ("binds",)


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
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
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


def test_tag_only_config_image_normalizes_but_preflight_must_refuse_it() -> None:
    raw = _inspect()
    raw["Config"]["Image"] = PINNED_IMAGE_REF
    snapshot = normalize_raw_inspect(raw)
    assert snapshot.image == PINNED_IMAGE_REF
    assert snapshot.resolved_image_id == PINNED_IMAGE_ID
    argv = build_recreate_argv(snapshot, replacement_name="nhms-db")
    assert argv[-2] == PINNED_IMAGE_ID
    assert snapshot.image != argv[-2]
