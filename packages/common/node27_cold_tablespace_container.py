"""Inert exact-container snapshot, recreation argv, and rollback decisions.

The production installer hands raw ``docker inspect`` JSON to this module.  It
never shell-sources that data.  Unsupported non-default fields are refusals: a
recreate is safer to reject than to silently normalize an old container away.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from packages.common.node27_cold_tablespace_identity import (
    CONTAINER_COLD_PATH,
    PRODUCTION_CONTAINER,
    PRODUCTION_HOST_PATH,
    PRODUCTION_IDENTITY,
    ColdTablespaceIdentity,
    validate_identity_for_action,
)

# Compatibility exports.  New state-machine code carries ``ColdTablespaceIdentity``
# instead of selecting these production literals independently.
LIVE_CONTAINER = PRODUCTION_CONTAINER
COLD_HOST_PATH = str(PRODUCTION_HOST_PATH)
COLD_CONTAINER_PATH = CONTAINER_COLD_PATH
COLD_BIND = PRODUCTION_IDENTITY.cold_bind
MAX_INSPECT_BYTES = 256 * 1024


class ContainerContractError(ValueError):
    """Raw Docker state is unsafe, malformed, or cannot be recreated exactly."""


@dataclass(frozen=True)
class ContainerSnapshot:
    container_id: str
    name: str
    image: str
    resolved_image_id: str
    environment: tuple[str, ...]
    command: tuple[str, ...]
    entrypoint: tuple[str, ...] | None
    working_dir: str
    user: str
    labels: tuple[tuple[str, str], ...]
    stop_signal: str | None
    healthcheck: Mapping[str, Any] | None
    binds: tuple[str, ...]
    ports: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    restart_policy: tuple[str, int]
    nano_cpus: int
    memory: int
    memory_swap: int
    shm_size: int
    stop_timeout: int
    readonly_rootfs: bool
    cap_add: tuple[str, ...]
    cap_drop: tuple[str, ...]
    security_opt: tuple[str, ...]
    network_mode: str
    privileged: bool
    publish_all_ports: bool
    auto_remove: bool
    volumes_from: tuple[str, ...]
    devices: tuple[str, ...]
    device_requests: tuple[Any, ...]
    tmpfs: tuple[tuple[str, str], ...]
    extra_hosts: tuple[str, ...]
    masked_paths: tuple[str, ...]
    readonly_paths: tuple[str, ...]
    running: bool | None = None

    @property
    def config_digest(self) -> str:
        """Digest reconstructible configuration, never the Docker instance ID."""

        return hashlib.sha256(_canonical(self.config_payload())).hexdigest()

    def config_payload(self) -> dict[str, Any]:
        """Every field reproduced by ``build_recreate_argv`` except runtime ID."""

        return {
            "image": self.image,
            "resolved_image_id": self.resolved_image_id,
            "environment": list(self.environment),
            "command": list(self.command),
            "entrypoint": None if self.entrypoint is None else list(self.entrypoint),
            "working_dir": self.working_dir,
            "user": self.user,
            "labels": dict(self.labels),
            "stop_signal": self.stop_signal,
            "healthcheck": self.healthcheck,
            "binds": list(self.binds),
            "ports": {key: [{"HostIp": host, "HostPort": port} for host, port in values] for key, values in self.ports},
            "restart_policy": {"Name": self.restart_policy[0], "MaximumRetryCount": self.restart_policy[1]},
            "nano_cpus": self.nano_cpus,
            "memory": self.memory,
            "memory_swap": self.memory_swap,
            "shm_size": self.shm_size,
            "stop_timeout": self.stop_timeout,
            "readonly_rootfs": self.readonly_rootfs,
            "cap_add": list(self.cap_add),
            "cap_drop": list(self.cap_drop),
            "security_opt": list(self.security_opt),
            "network_mode": self.network_mode,
            "privileged": self.privileged,
            "publish_all_ports": self.publish_all_ports,
            "auto_remove": self.auto_remove,
            "volumes_from": list(self.volumes_from),
            "devices": list(self.devices),
            "device_requests": list(self.device_requests),
            "tmpfs": dict(self.tmpfs),
            "extra_hosts": list(self.extra_hosts),
            "masked_paths": list(self.masked_paths),
            "readonly_paths": list(self.readonly_paths),
        }

    def private_payload(self) -> dict[str, Any]:
        """Exact observed identity, including names that config digest excludes."""

        return {"container_id": self.container_id, "name": self.name, **self.config_payload()}

    def public_payload(self) -> dict[str, Any]:
        private = self.private_payload()
        private.pop("environment")
        private.pop("running", None)
        private["environment_names"] = sorted(item.split("=", 1)[0] for item in self.environment)
        private["config_digest"] = self.config_digest
        return private

    def with_cold_bind(self, *, identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY) -> ContainerSnapshot:
        validate_identity_for_action(identity)
        if identity.cold_bind in self.binds:
            return self
        return ContainerSnapshot(**{**self.__dict__, "binds": tuple(sorted((*self.binds, identity.cold_bind)))})


@dataclass(frozen=True)
class ContainerDiff:
    approved: bool
    changed_fields: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RollbackPlan:
    remove_catalog: bool
    remove_installer_container: bool
    restore_prior: bool
    remove_host_path: bool
    blockers: tuple[str, ...]


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContainerContractError(f"{label} must be an object")
    return value


def _string(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContainerContractError(f"{label} must be a non-empty string")
    return value


def _strings(value: object, *, label: str, allow_none: bool = False) -> tuple[str, ...] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContainerContractError(f"{label} must be a string array")
    return tuple(value)


def _int(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContainerContractError(f"{label} must be a non-negative integer")
    return value


_DOCKER_DEFAULT_MASKED_BASE = (
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
)
_CPU_THERMAL_PATH = re.compile(r"^/sys/devices/system/cpu/cpu(?P<index>0|[1-9][0-9]*)/thermal_throttle$")
_DOCKER_DEFAULT_READONLY_PATHS = (
    "/proc/bus",
    "/proc/fs",
    "/proc/irq",
    "/proc/sys",
    "/proc/sysrq-trigger",
)


def _known_default_config(key: str, item: object) -> bool:
    if key == "Hostname":
        # Docker derives this from the new instance ID unless --hostname was
        # supplied.  A user-selected hostname is deliberately not accepted.
        return isinstance(item, str) and len(item) in {12, 64} and all(ch in "0123456789abcdef" for ch in item.lower())
    if key == "ExposedPorts":
        return isinstance(item, Mapping) and all(
            isinstance(port, str) and isinstance(value, Mapping) and not value for port, value in item.items()
        )
    return key in {"ArgsEscaped", "Shell"} and item in (False, None)


def _known_default_host(key: str, item: object) -> bool:
    if key in {"CgroupnsMode", "IpcMode"}:
        return item == "private"
    if key == "Runtime":
        return item == "runc"
    if key == "ConsoleSize":
        return item == [0, 0]
    if key == "LogConfig":
        return item == {"Type": "json-file", "Config": {}}
    return False


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    label: str,
    known_default: Callable[[str, object], bool] | None = None,
) -> None:
    unsupported = sorted(
        key
        for key, item in value.items()
        if key not in allowed
        and item not in (None, False, 0, "", [], {})
        and not (known_default is not None and known_default(key, item))
    )
    if unsupported:
        raise ContainerContractError(f"{label} contains unsupported non-default fields: {', '.join(unsupported)}")


def _normalized_ports(raw: object) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    ports = _mapping(raw, label="HostConfig.PortBindings")
    result: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for container_port, bindings in sorted(ports.items()):
        if not isinstance(container_port, str) or not isinstance(bindings, list):
            raise ContainerContractError("HostConfig.PortBindings is malformed")
        entries: list[tuple[str, str]] = []
        for binding in bindings:
            item = _mapping(binding, label="HostConfig.PortBindings item")
            _require_exact_keys(item, allowed=frozenset({"HostIp", "HostPort"}), label="HostConfig.PortBindings item")
            entries.append(
                (
                    _string(item.get("HostIp"), label="HostIp", allow_empty=True),
                    _string(item.get("HostPort"), label="HostPort", allow_empty=True),
                )
            )
        result.append((container_port, tuple(sorted(entries))))
    return tuple(result)


def _normalized_bind(bind: str) -> str:
    pieces = bind.split(":")
    if any(not piece for piece in pieces) or len(pieces) not in {2, 3}:
        raise ContainerContractError("HostConfig.Binds contains unsupported bind syntax")
    source, destination = pieces[0], pieces[1]
    if len(pieces) == 2:
        return f"{source}:{destination}:rw"
    mode = pieces[2]
    if mode in {"rw", "ro"}:
        return bind
    raise ContainerContractError("HostConfig.Binds contains unsupported bind options")


def _normalized_binds(raw: object) -> tuple[str, ...]:
    binds = _strings(raw, label="HostConfig.Binds")
    assert binds is not None
    normalized = tuple(sorted(_normalized_bind(bind) for bind in binds))
    identities = tuple(item.rsplit(":", 1)[0] for item in normalized)
    if len(set(identities)) != len(identities):
        raise ContainerContractError("HostConfig.Binds contains duplicate bind identities")
    return normalized


def _unique_string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    items = _strings(value, label=label) or ()
    if len(set(items)) != len(items):
        raise ContainerContractError(f"{label} contains duplicate paths")
    return items


def _normalized_masked_paths(raw: object) -> tuple[str, ...]:
    # Docker 28.2.2 derives these isolation paths from the daemon; they are not
    # reconstructed as CLI flags.  Recreate still requires exact observed equality.
    if raw in (None, []):
        return ()
    paths = _unique_string_tuple(raw, label="HostConfig.MaskedPaths")
    base_count = len(_DOCKER_DEFAULT_MASKED_BASE)
    if paths[:base_count] != _DOCKER_DEFAULT_MASKED_BASE:
        raise ContainerContractError("HostConfig.MaskedPaths does not match the Docker default isolation shape")
    thermal: list[int] = []
    for path in paths[base_count:]:
        matched = _CPU_THERMAL_PATH.fullmatch(path)
        if matched is None:
            raise ContainerContractError("HostConfig.MaskedPaths contains a non-default isolation path")
        thermal.append(int(matched.group("index")))
    if thermal != list(range(len(thermal))):
        raise ContainerContractError("HostConfig.MaskedPaths CPU thermal paths are not contiguous from cpu0")
    return paths


def _normalized_readonly_paths(raw: object) -> tuple[str, ...]:
    # Docker 28.2.2 derives these isolation paths from the daemon; they are not
    # reconstructed as CLI flags.  Recreate still requires exact observed equality.
    if raw in (None, []):
        return ()
    paths = _unique_string_tuple(raw, label="HostConfig.ReadonlyPaths")
    if paths != _DOCKER_DEFAULT_READONLY_PATHS:
        raise ContainerContractError("HostConfig.ReadonlyPaths does not match the Docker default isolation set")
    return paths


def _normalized_memory_swap(raw: object, *, memory: int) -> int:
    if raw in (None, 0):
        return 0
    swap = _int(raw, label="HostConfig.MemorySwap")
    if memory <= 0 or swap < memory:
        raise ContainerContractError("HostConfig.MemorySwap requires a compatible Memory contract")
    return swap


_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _resolved_image_id(document: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    raw = document.get("Image")
    if not isinstance(raw, str) or not _SHA256_ID.fullmatch(raw):
        raise ContainerContractError("document-level Image must be a sha256 identity")
    image_ref = config.get("Image")
    if not isinstance(image_ref, str) or not image_ref:
        raise ContainerContractError("Config.Image must be a reconstructible image reference")
    return raw


def _normalized_stop_timeout(config: Mapping[str, Any], host: Mapping[str, Any]) -> int:
    config_raw = config.get("StopTimeout")
    host_raw = host.get("StopTimeout")
    if config_raw not in (None, 0) and host_raw not in (None, 0) and config_raw != host_raw:
        raise ContainerContractError("Config.StopTimeout conflicts with HostConfig.StopTimeout")
    if config_raw not in (None, 0):
        return _int(config_raw, label="Config.StopTimeout")
    return _int(host_raw or 0, label="HostConfig.StopTimeout")


def _running_state(document: Mapping[str, Any]) -> bool | None:
    state = document.get("State")
    if not isinstance(state, Mapping) or "Running" not in state:
        return None
    running = state.get("Running")
    if type(running) is not bool:
        raise ContainerContractError("State.Running must be a boolean when present")
    return running


def normalize_raw_inspect(raw: object, *, max_bytes: int = MAX_INSPECT_BYTES) -> ContainerSnapshot:
    """Normalize bounded raw inspect data without executing or shell-parsing it."""

    encoded = _canonical(raw)
    if len(encoded) > max_bytes:
        raise ContainerContractError("docker inspect exceeds the byte ceiling")
    document = _mapping(raw, label="docker inspect")
    config = _mapping(document.get("Config"), label="Config")
    host = _mapping(document.get("HostConfig"), label="HostConfig")
    # As with HostConfig below, this is deliberately limited to values whose
    # exact ``docker run`` spelling is owned by this module.  Docker's default
    # inspect document contains additional false/empty keys, which the generic
    # non-default check safely ignores.
    _require_exact_keys(
        config,
        allowed=frozenset(
            {
                "Image",
                "Env",
                "Cmd",
                "Entrypoint",
                "WorkingDir",
                "User",
                "Labels",
                "StopSignal",
                "Healthcheck",
                "StopTimeout",
            }
        ),
        label="Config",
        known_default=_known_default_config,
    )
    # Every admitted non-default HostConfig field must be represented by
    # ``ContainerSnapshot`` and rebuilt below.  Docker exposes many more fields
    # than this narrow contract; accepting one merely because its key is known
    # would silently discard a production limit or isolation control on recreate.
    _require_exact_keys(
        host,
        allowed=frozenset(
            {
                "Binds",
                "PortBindings",
                "RestartPolicy",
                "NanoCpus",
                "Memory",
                "MemorySwap",
                "ShmSize",
                "StopTimeout",
                "ReadonlyRootfs",
                "CapAdd",
                "CapDrop",
                "SecurityOpt",
                "NetworkMode",
                "Privileged",
                "PublishAllPorts",
                "AutoRemove",
                "VolumesFrom",
                "Devices",
                "DeviceRequests",
                "Tmpfs",
                "ExtraHosts",
                "MaskedPaths",
                "ReadonlyPaths",
            }
        ),
        label="HostConfig",
        known_default=_known_default_host,
    )
    restart = _mapping(host.get("RestartPolicy") or {}, label="HostConfig.RestartPolicy")
    _require_exact_keys(restart, allowed=frozenset({"Name", "MaximumRetryCount"}), label="HostConfig.RestartPolicy")
    labels = _mapping(config.get("Labels") or {}, label="Config.Labels")
    healthcheck = config.get("Healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, Mapping):
        raise ContainerContractError("Config.Healthcheck must be an object or null")
    device_requests = host.get("DeviceRequests") or []
    if not isinstance(device_requests, list):
        raise ContainerContractError("HostConfig.DeviceRequests must be an array")
    tmpfs = _mapping(host.get("Tmpfs") or {}, label="HostConfig.Tmpfs")
    return ContainerSnapshot(
        container_id=_string(document.get("Id"), label="Id"),
        name=_string(document.get("Name"), label="Name").lstrip("/"),
        image=_string(config.get("Image"), label="Config.Image"),
        resolved_image_id=_resolved_image_id(document, config),
        environment=tuple(sorted(_strings(config.get("Env"), label="Config.Env") or ())),
        command=tuple(_strings(config.get("Cmd"), label="Config.Cmd") or ()),
        entrypoint=_strings(config.get("Entrypoint"), label="Config.Entrypoint", allow_none=True),
        working_dir=_string(config.get("WorkingDir") or "/", label="Config.WorkingDir"),
        user=_string(config.get("User") or "", label="Config.User", allow_empty=True),
        labels=tuple(
            sorted(
                (_string(key, label="Config.Labels key"), _string(value, label="Config.Labels value", allow_empty=True))
                for key, value in labels.items()
            )
        ),
        stop_signal=None
        if config.get("StopSignal") is None
        else _string(config.get("StopSignal"), label="Config.StopSignal"),
        healthcheck=None if healthcheck is None else json.loads(_canonical(healthcheck)),
        binds=_normalized_binds(host.get("Binds") or []),
        ports=_normalized_ports(host.get("PortBindings") or {}),
        restart_policy=(
            _string(restart.get("Name") or "", label="RestartPolicy.Name", allow_empty=True),
            _int(restart.get("MaximumRetryCount") or 0, label="RestartPolicy.MaximumRetryCount"),
        ),
        nano_cpus=_int(host.get("NanoCpus") or 0, label="HostConfig.NanoCpus"),
        memory=_int(host.get("Memory") or 0, label="HostConfig.Memory"),
        memory_swap=_normalized_memory_swap(
            host.get("MemorySwap"), memory=_int(host.get("Memory") or 0, label="HostConfig.Memory")
        ),
        shm_size=_int(host.get("ShmSize") or 0, label="HostConfig.ShmSize"),
        stop_timeout=_normalized_stop_timeout(config, host),
        readonly_rootfs=bool(host.get("ReadonlyRootfs") or False),
        cap_add=tuple(sorted(_strings(host.get("CapAdd") or [], label="HostConfig.CapAdd") or ())),
        cap_drop=tuple(sorted(_strings(host.get("CapDrop") or [], label="HostConfig.CapDrop") or ())),
        security_opt=tuple(sorted(_strings(host.get("SecurityOpt") or [], label="HostConfig.SecurityOpt") or ())),
        network_mode=_string(host.get("NetworkMode") or "default", label="HostConfig.NetworkMode"),
        privileged=bool(host.get("Privileged") or False),
        publish_all_ports=bool(host.get("PublishAllPorts") or False),
        auto_remove=bool(host.get("AutoRemove") or False),
        volumes_from=tuple(sorted(_strings(host.get("VolumesFrom") or [], label="HostConfig.VolumesFrom") or ())),
        devices=tuple(sorted(_strings(host.get("Devices") or [], label="HostConfig.Devices") or ())),
        device_requests=tuple(json.loads(_canonical(item)) for item in device_requests),
        tmpfs=tuple(
            sorted(
                (_string(key, label="HostConfig.Tmpfs key"), _string(value, label="HostConfig.Tmpfs value"))
                for key, value in tmpfs.items()
            )
        ),
        extra_hosts=tuple(sorted(_strings(host.get("ExtraHosts") or [], label="HostConfig.ExtraHosts") or ())),
        masked_paths=_normalized_masked_paths(host.get("MaskedPaths")),
        readonly_paths=_normalized_readonly_paths(host.get("ReadonlyPaths")),
        running=_running_state(document),
    )


def _docker_option(argv: list[str], flag: str, value: str) -> None:
    argv.extend((flag, value))


def build_recreate_argv(
    snapshot: ContainerSnapshot,
    *,
    replacement_name: str | None = None,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> tuple[str, ...]:
    """Build direct argv from the immutable identity contract.

    ``replacement_name`` remains only as a compatibility assertion for existing
    callers.  It cannot redirect a production invocation and a disposable test
    must pass an identity issued by ``make_disposable_identity``.
    """

    validate_identity_for_action(identity)
    expected_name = identity.container_name
    if replacement_name is not None and replacement_name != expected_name:
        raise ContainerContractError("replacement container name differs from the immutable identity contract")
    argv = [identity.docker_bin, "run", "-d", "--name", expected_name]
    if snapshot.user:
        _docker_option(argv, "--user", snapshot.user)
    if snapshot.working_dir != "/":
        _docker_option(argv, "--workdir", snapshot.working_dir)
    if snapshot.stop_signal:
        _docker_option(argv, "--stop-signal", snapshot.stop_signal)
    if snapshot.stop_timeout:
        _docker_option(argv, "--stop-timeout", str(snapshot.stop_timeout))
    restart_name, restart_retries = snapshot.restart_policy
    if restart_name:
        restart = restart_name if restart_name != "on-failure" else f"on-failure:{restart_retries}"
        _docker_option(argv, "--restart", restart)
    if snapshot.nano_cpus:
        _docker_option(argv, "--cpus", _cpu_text(snapshot.nano_cpus))
    if snapshot.memory:
        _docker_option(argv, "--memory", str(snapshot.memory))
    if snapshot.memory_swap:
        if snapshot.memory <= 0 or snapshot.memory_swap < snapshot.memory:
            raise ContainerContractError("HostConfig.MemorySwap requires a compatible Memory contract")
        _docker_option(argv, "--memory-swap", str(snapshot.memory_swap))
    if snapshot.shm_size:
        _docker_option(argv, "--shm-size", str(snapshot.shm_size))
    if snapshot.readonly_rootfs:
        argv.append("--read-only")
    if snapshot.privileged:
        argv.append("--privileged")
    if snapshot.network_mode != "default":
        _docker_option(argv, "--network", snapshot.network_mode)
    if snapshot.publish_all_ports:
        argv.append("-P")
    if snapshot.auto_remove:
        argv.append("--rm")
    for value in snapshot.cap_add:
        _docker_option(argv, "--cap-add", value)
    for value in snapshot.cap_drop:
        _docker_option(argv, "--cap-drop", value)
    for value in snapshot.security_opt:
        _docker_option(argv, "--security-opt", value)
    for value in snapshot.extra_hosts:
        _docker_option(argv, "--add-host", value)
    for key, values in snapshot.ports:
        for host, port in values:
            _docker_option(argv, "-p", f"{host}:{port}:{key.split('/', 1)[0]}")
    for name, value in snapshot.labels:
        _docker_option(argv, "--label", f"{name}={value}")
    for value in snapshot.environment:
        _docker_option(argv, "--env", value)
    for value in (*snapshot.binds, *(item for item in (identity.cold_bind,) if item not in snapshot.binds)):
        _docker_option(argv, "--volume", value)
    for value in snapshot.volumes_from:
        _docker_option(argv, "--volumes-from", value)
    for value in snapshot.devices:
        _docker_option(argv, "--device", value)
    for target, options in snapshot.tmpfs:
        _docker_option(argv, "--tmpfs", f"{target}:{options}")
    if snapshot.entrypoint is not None:
        if len(snapshot.entrypoint) != 1:
            raise ContainerContractError("multi-argument Entrypoint cannot be reproduced exactly with docker run")
        _docker_option(argv, "--entrypoint", snapshot.entrypoint[0])
    if snapshot.healthcheck is not None:
        raise ContainerContractError("Healthcheck cannot be reproduced exactly with the supported direct argv contract")
    if snapshot.device_requests:
        raise ContainerContractError(
            "DeviceRequests cannot be reproduced exactly with the supported direct argv contract"
        )
    argv.append(snapshot.resolved_image_id)
    argv.extend(snapshot.command)
    return tuple(argv)


def _cpu_text(nano_cpus: int) -> str:
    value = nano_cpus / 1_000_000_000
    return str(int(value)) if value.is_integer() else format(value, "g")


def diff_container_config(
    before: ContainerSnapshot,
    after: ContainerSnapshot,
    *,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> ContainerDiff:
    """Accept a new runtime container ID but no config drift beyond one bind."""

    validate_identity_for_action(identity)
    if before.resolved_image_id != after.resolved_image_id:
        return ContainerDiff(
            approved=False,
            changed_fields=("resolved_image_id",),
            blockers=("resolved image identity drifted",),
        )
    before_payload = before.config_payload()
    after_payload = after.config_payload()
    changed = tuple(sorted(key for key in before_payload if before_payload[key] != after_payload.get(key)))
    expected_binds = tuple(sorted((*before.binds, identity.cold_bind)))
    approved = changed == ("binds",) and after.binds == expected_binds
    blockers = () if approved else ("container diff is not exactly the one cold bind",)
    return ContainerDiff(approved=approved, changed_fields=changed, blockers=blockers)


def rollback_plan(
    *,
    installer_container: str,
    prior_container: str,
    installer_created_catalog: bool,
    catalog_dependents: int,
    pg_tblspc_references: Sequence[str],
    current_bind_references: Sequence[str],
    stopped_bind_references: Sequence[str],
    host_path_identity_matches: bool,
    host_path_empty: bool,
    installer_container_created: bool = True,
) -> RollbackPlan:
    if not installer_container or not prior_container:
        raise ContainerContractError("rollback container identities are required")
    blockers: list[str] = []
    if catalog_dependents:
        blockers.append("catalog has dependents")
    if pg_tblspc_references:
        blockers.append("pg_tblspc still references host state")
    if current_bind_references:
        blockers.append("live container still references host state")
    if stopped_bind_references:
        blockers.append("stopped container has a stale host bind")
    if not host_path_identity_matches:
        blockers.append("host path identity is uncertain")
    if not host_path_empty:
        blockers.append("host path is not empty")
    return RollbackPlan(
        remove_catalog=installer_created_catalog and catalog_dependents == 0,
        remove_installer_container=installer_container_created,
        restore_prior=True,
        remove_host_path=not blockers,
        blockers=tuple(blockers),
    )
