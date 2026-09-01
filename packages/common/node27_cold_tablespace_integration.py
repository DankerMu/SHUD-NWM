"""Disposable Docker boundaries for the cold-tablespace installer oracle.

This module never installs a tablespace itself.  It builds an owned synthetic
identity and dependencies, then the marker test calls the same public
``run_install`` core used by production.  Every action refuses live names,
paths, ports and image authority before Docker, filesystem, or PostgreSQL work.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_residency import (
    PINNED_IMAGE_ID,
    PINNED_IMAGE_REF,
    PINNED_PG_VERSION_PREFIX,
    PINNED_TIMESCALEDB_VERSION,
)
from packages.common.node27_cold_tablespace_evidence import (
    EvidencePolicy,
    parse_backup_inventory,
    verify_root_storage_evidence,
)
from packages.common.node27_cold_tablespace_identity import (
    INTEGRATION_PREFIX,
    ColdTablespaceIdentity,
    IdentityContractError,
    assert_disposable_absent,
    make_disposable_identity,
    validate_identity_for_action,
)
from packages.common.node27_cold_tablespace_root_capability import (
    KNOWN_WORK_ROOT_CHILDREN,
    RootCapabilityError,
    RootEvidenceCapability,
    assert_root_helpers_absent,
    pinned_image_root_argv,
    probe_root_evidence_capability,
)
from packages.common.node27_cold_tablespace_types import InstallDependencies

DEFAULT_HOST_PORT = 55494
_CONTAINER_PGDATA = "/home/postgres/pgdata/data"


class ColdTablespaceIntegrationError(RuntimeError):
    """The disposable oracle is unsafe or did not produce the required proof."""


@dataclass(frozen=True)
class IntegrationConfig:
    identity: ColdTablespaceIdentity
    password: str

    @property
    def container_name(self) -> str:
        return self.identity.container_name

    @property
    def prior_container_name(self) -> str:
        return self.identity.prior_container_name

    @property
    def host_port(self) -> int:
        return self.identity.host_port

    @property
    def work_root(self) -> Path:
        assert self.identity.work_root is not None
        return self.identity.work_root

    @property
    def image_id(self) -> str:
        assert self.identity.image_id is not None
        return self.identity.image_id

    @property
    def image_ref(self) -> str:
        assert self.identity.image_ref is not None
        return self.identity.image_ref

    @property
    def docker_bin(self) -> str:
        return self.identity.docker_bin


@dataclass
class IntegrationResources:
    config: IntegrationConfig
    capability: RootEvidenceCapability | None = None
    created_work_root: bool = False
    known_containers: set[str] = field(default_factory=set)
    env_path: Path | None = None
    actions: list[tuple[str, ...]] = field(default_factory=list)

    def require_capability(self) -> RootEvidenceCapability:
        if self.capability is None:
            raise ColdTablespaceIntegrationError("root evidence capability was not established before resources")
        return self.capability


def default_config(*, work_root: Path | None = None, host_port: int | None = None) -> IntegrationConfig:
    token = uuid.uuid4().hex[:12]
    root = work_root or Path("/tmp") / f"{INTEGRATION_PREFIX}{token}"
    if work_root is not None:
        name = root.name
        if not name.startswith(INTEGRATION_PREFIX):
            raise ColdTablespaceIntegrationError("disposable work root lacks the #1894 ownership prefix")
        token = name.removeprefix(INTEGRATION_PREFIX)
    chosen_port = _allocate_host_port() if host_port is None else host_port
    identity = make_disposable_identity(
        container_name=f"{INTEGRATION_PREFIX}{token}",
        prior_container_name=f"{INTEGRATION_PREFIX}{token}-before",
        host_port=chosen_port,
        work_root=root,
        host_path=root / "cold",
        image_id=PINNED_IMAGE_ID,
        image_ref=PINNED_IMAGE_REF,
    )
    return IntegrationConfig(identity=identity, password=f"nhms-1894-disposable-{uuid.uuid4().hex}")


def _allocate_host_port() -> int:
    """Reserve an ephemeral loopback port identity before disposable setup."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == 55432:
        raise ColdTablespaceIntegrationError("ephemeral disposable port collided with the live PostgreSQL port")
    return port


def validate_isolated_config(config: IntegrationConfig) -> None:
    try:
        validate_identity_for_action(config.identity)
    except IdentityContractError as error:
        raise ColdTablespaceIntegrationError(str(error)) from error
    if config.identity.kind != "synthetic":
        raise ColdTablespaceIntegrationError("disposable oracle requires a synthetic identity")
    if not config.password or len(config.password) < 16:
        raise ColdTablespaceIntegrationError("disposable PostgreSQL password is invalid")


def _run(argv: Sequence[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=False, capture_output=True, text=True, timeout=timeout)


def _checked(runner: Callable[..., subprocess.CompletedProcess[str]], argv: Sequence[str], *, timeout: int = 90) -> str:
    result = runner(argv, timeout=timeout)
    if result.returncode != 0:
        raise ColdTablespaceIntegrationError(
            f"disposable Docker operation failed: {argv[1] if len(argv) > 1 else 'unknown'}"
        )
    return result.stdout


def _container_exists(
    config: IntegrationConfig, name: str, *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> bool:
    return runner((config.docker_bin, "inspect", name), timeout=20).returncode == 0


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _prepare_absence(config: IntegrationConfig, *, runner: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    try:
        assert_disposable_absent(
            config.identity,
            path_exists=Path.exists,
            container_exists=lambda name: _container_exists(config, name, runner=runner),
            port_is_available=_port_is_available,
        )
    except IdentityContractError as error:
        raise ColdTablespaceIntegrationError(str(error)) from error


def prepare_resources(
    config: IntegrationConfig,
    *,
    capability: RootEvidenceCapability | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> IntegrationResources:
    validate_isolated_config(config)
    resolved_capability = capability or require_root_evidence_capability(config, runner=runner)
    _prepare_absence(config, runner=runner)
    root = config.work_root
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    # ``cold`` intentionally remains absent here.  The common installer writes
    # authority first, then its injected root helper creates it as the recorded
    # ``path_created`` mutation.
    for child in (root / "pgdata", root / "receipts"):
        child.mkdir(mode=0o700)
        child.chmod(0o700)
    resources = IntegrationResources(config=config, capability=resolved_capability, created_work_root=True)
    env_path = root / "postgres.env"
    env_path.write_text(
        "POSTGRES_USER=postgres\n"
        f"POSTGRES_PASSWORD={config.password}\n"
        "POSTGRES_DB=postgres\n"
        f"PGDATA={_CONTAINER_PGDATA}\n"
        "TIMESCALEDB_TELEMETRY=off\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    resources.env_path = env_path
    return resources


def _resource_args(resources: IntegrationResources, *, action: str) -> tuple[str, ...]:
    config = resources.config
    capability = resources.require_capability()
    if action not in {"prepare", "render", "seal", "create-cold-path", "cleanup"}:
        raise ColdTablespaceIntegrationError("root evidence helper action is invalid")
    return (
        "--action",
        action,
        "--work-root",
        str(config.work_root),
        "--cold-path",
        str(config.identity.host_path),
        "--pgdata",
        str(config.work_root / "pgdata"),
        "--evidence-root",
        str(config.work_root / "evidence"),
        "--container-name",
        config.container_name,
        "--prior-container-name",
        config.prior_container_name,
        "--host-port",
        str(config.host_port),
        "--image-id",
        capability.image_id,
        "--image-ref",
        capability.image_ref,
        "--hostname",
        f"nhms-1894-{socket.gethostname()}",
        "--runtime-uid",
        str(capability.runtime_uid),
        "--runtime-gid",
        str(capability.runtime_gid),
        "--reader-gid",
        str(os.getgid()),
    )


def root_evidence_setup_argv(resources: IntegrationResources, *, action: str) -> tuple[str, ...]:
    """Direct sudo argv for the checked-in root helper; never shell-substitute it."""

    helper = Path(__file__).resolve().parents[2] / "scripts/node27_cold_tablespace_root_evidence_setup.py"
    return ("/usr/bin/sudo", "-n", sys.executable, os.fspath(helper), *_resource_args(resources, action=action))


def host_evidence_render_argv(resources: IntegrationResources) -> tuple[str, ...]:
    """Host Python renders inert evidence only; it is never run in the image."""

    helper = Path(__file__).resolve().parents[2] / "scripts/node27_cold_tablespace_root_evidence_setup.py"
    return (sys.executable, os.fspath(helper), *_resource_args(resources, action="render"))


def require_root_evidence_capability(
    config: IntegrationConfig, *, runner: Callable[..., subprocess.CompletedProcess[str]] = _run
) -> RootEvidenceCapability:
    """Measure exact image identity before resources, then prefer sudo or prove isolated image root."""

    try:
        return probe_root_evidence_capability(config.identity, runner=runner)
    except RootCapabilityError as error:
        raise ColdTablespaceIntegrationError(str(error)) from error


def _root_action(
    resources: IntegrationResources,
    *,
    action: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    capability = resources.require_capability()
    if capability.strategy == "sudo":
        _checked(runner, root_evidence_setup_argv(resources, action=action), timeout=60)
        return
    try:
        argv = pinned_image_root_argv(
            resources.config.identity,
            capability=capability,
            work_root=resources.config.work_root,
            reader_gid=os.getgid(),
            action=action,
        )
        _checked(runner, argv, timeout=60)
    except RootCapabilityError as error:
        raise ColdTablespaceIntegrationError(str(error)) from error
    try:
        assert_root_helpers_absent(resources.config.identity, runner=runner)
    except RootCapabilityError as error:
        raise ColdTablespaceIntegrationError(str(error)) from error


def root_evidence_ready(
    resources: IntegrationResources,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> dict[str, Any]:
    """Render synthetic documents on the host, seal as root, then parse exact evidence."""

    config = resources.config
    capability = resources.require_capability()
    if capability.strategy == "pinned_image":
        _checked(runner, host_evidence_render_argv(resources), timeout=60)
        _root_action(resources, action="prepare", runner=runner)
    else:
        _root_action(resources, action="prepare", runner=runner)
    policy = EvidencePolicy(
        expected_hostname=f"nhms-1894-{socket.gethostname()}",
        array_device="/dev/md0",
        max_age_seconds=300,
        expected_uid=0,
        approved_modes=(0o640,),
        mdadm_argv=("/usr/sbin/mdadm", "--detail", "/dev/md0"),
        smartctl_prefix=("/usr/sbin/smartctl",),
        backup_argv=("/usr/local/sbin/nhms-backup-inventory", "--json"),
        expected_pgdata=str(config.work_root / "pgdata"),
    )
    evidence_root = config.work_root / "evidence"
    health = verify_root_storage_evidence(
        evidence_root / "mdadm.json",
        {device: evidence_root / f"smart-{Path(device).name}.json" for device in ("/dev/sdb1", "/dev/sdc1")},
        policy=policy,
        now=datetime.now(UTC),
    )
    if not health.healthy:
        raise ColdTablespaceIntegrationError("root-owned synthetic RAID/SMART evidence failed production parsing")
    backup = parse_backup_inventory(
        evidence_root / "backup.json",
        policy=policy,
        external_targets=(config.identity.container_path,),
        now=datetime.now(UTC),
    )
    if not backup.complete:
        raise ColdTablespaceIntegrationError("root-owned synthetic backup evidence failed production parsing")
    for name in ("mdadm.json", "smart-sdb1.json", "smart-sdc1.json", "backup.json"):
        info = os.stat(evidence_root / name, follow_symlinks=False)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o640:
            raise ColdTablespaceIntegrationError("root evidence owner/mode is not production-parser compatible")
    return {
        "healthy": health.healthy,
        "members": list(health.members),
        "raid": {
            "file_identity": health.raid.file_identity,
            "captured_at": health.raid.captured_at,
            "state": health.raid.state,
        },
        "smart": [
            {
                "device": item.device,
                "status": item.status,
                "file_identity": item.file_identity,
                "captured_at": item.captured_at,
            }
            for item in health.smart
        ],
        "blockers": list(health.blockers),
        "backup": {
            "complete": backup.complete,
            "covered_paths": list(backup.covered_paths),
            "missing_targets": list(backup.missing_targets),
            "file_identity": backup.file_identity,
            "blockers": list(backup.blockers),
        },
    }


def initial_container_argv(resources: IntegrationResources) -> tuple[str, ...]:
    config = resources.config
    capability = resources.require_capability()
    validate_isolated_config(config)
    return (
        config.docker_bin,
        "run",
        "-d",
        "--name",
        config.container_name,
        "--user",
        f"{capability.runtime_uid}:{capability.runtime_gid}",
        "--env-file",
        str(config.work_root / "postgres.env"),
        "--restart",
        "unless-stopped",
        "--memory",
        "536870912",
        "-p",
        f"127.0.0.1:{config.host_port}:5432",
        "-v",
        f"{config.work_root / 'pgdata'}:{_CONTAINER_PGDATA}:rw",
        config.image_id,
        "postgres",
        "-c",
        "shared_preload_libraries=timescaledb",
    )


def start_prior(
    resources: IntegrationResources, *, runner: Callable[..., subprocess.CompletedProcess[str]] = _run
) -> Mapping[str, Any]:
    argv = initial_container_argv(resources)
    _checked(runner, argv)
    resources.known_containers.add(resources.config.container_name)
    resources.actions.append(argv)
    wait_for_sql(resources.config)
    raw = inspect_container(resources.config, resources.config.container_name, runner=runner)
    used = raw.get("Image")
    configured = raw.get("Config", {}).get("Image") if isinstance(raw.get("Config"), Mapping) else None
    if used != PINNED_IMAGE_ID or configured not in {PINNED_IMAGE_ID, PINNED_IMAGE_REF}:
        raise ColdTablespaceIntegrationError("disposable Docker image differs from #1892 pinned authority")
    return raw


def inspect_container(
    config: IntegrationConfig,
    name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> Mapping[str, Any]:
    validate_isolated_config(config)
    raw = _checked(runner, (config.docker_bin, "inspect", name), timeout=30)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ColdTablespaceIntegrationError("disposable Docker inspect is malformed") from error
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], Mapping):
        raise ColdTablespaceIntegrationError("disposable Docker inspect must return exactly one object")
    return document[0]


def wait_for_sql(config: IntegrationConfig, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = "unattempted"
    while time.monotonic() < deadline:
        try:
            connection = connect(config)
            try:
                execute(connection, "SELECT 1")
                return
            finally:
                connection.close()
        except Exception as error:  # noqa: BLE001 - external Docker/PG boundary
            last = type(error).__name__
            time.sleep(0.5)
    raise ColdTablespaceIntegrationError(f"disposable PostgreSQL did not become ready ({last})")


def connect(config: IntegrationConfig) -> Any:
    validate_isolated_config(config)
    import psycopg2
    from psycopg2.extras import RealDictCursor

    connection = psycopg2.connect(
        host="127.0.0.1",
        port=config.host_port,
        user="postgres",
        password=config.password,
        dbname="postgres",
        connect_timeout=5,
        cursor_factory=RealDictCursor,
        application_name="nhms-1894-tablespace-oracle",
    )
    connection.autocommit = True
    return connection


def execute(connection: Any, sql: str, params: Sequence[object] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return []
        return [dict(row) for row in cursor.fetchall()]


def assert_engine_identity(config: IntegrationConfig, *, connection: Any | None = None) -> None:
    owns = connection is None
    active = connect(config) if owns else connection
    assert active is not None
    try:
        rows = execute(
            active, "SELECT version() AS pg, extversion AS ts FROM pg_extension WHERE extname = 'timescaledb'"
        )
    finally:
        if owns:
            active.close()
    if len(rows) != 1 or not str(rows[0]["pg"]).startswith("PostgreSQL " + PINNED_PG_VERSION_PREFIX):
        raise ColdTablespaceIntegrationError("disposable PostgreSQL version differs from #1892 pin")
    if str(rows[0]["ts"]) != PINNED_TIMESCALEDB_VERSION:
        raise ColdTablespaceIntegrationError("disposable TimescaleDB version differs from #1892 pin")


def bootstrap_business_tables(config: IntegrationConfig) -> None:
    """Create both production-named hypertables before the installer runs."""

    connection = connect(config)
    try:
        execute(connection, "CREATE EXTENSION IF NOT EXISTS timescaledb")
        assert_engine_identity(config, connection=connection)
        for schema, table in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")):
            execute(connection, f"CREATE SCHEMA IF NOT EXISTS {schema}")
            execute(
                connection,
                f"CREATE TABLE {schema}.{table} ("
                "id integer NOT NULL, valid_time timestamptz NOT NULL, "
                "value double precision NOT NULL, PRIMARY KEY (id, valid_time))",
            )
            execute(
                connection,
                f"SELECT create_hypertable('{schema}.{table}', 'valid_time', chunk_time_interval => interval '1 day')",
            )
            execute(
                connection,
                f"ALTER TABLE {schema}.{table} SET (timescaledb.compress, "
                "timescaledb.compress_segmentby = 'id', "
                "timescaledb.compress_orderby = 'valid_time')",
            )
            execute(
                connection,
                f"INSERT INTO {schema}.{table} VALUES "
                "(1, '2026-01-01T00:00:00Z', 1.0), "
                "(1, '2026-01-03T00:00:00Z', 2.0)",
            )
            execute(connection, f"SELECT compress_chunk(chunk) FROM show_chunks('{schema}.{table}') AS chunk")
    finally:
        connection.close()


def _host_path(config: IntegrationConfig) -> dict[str, Any]:
    path = config.identity.host_path
    usage = os.statvfs(path.parent if not path.exists() else path)
    info = path.stat() if path.exists() else None
    return {
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "is_directory": path.is_dir(),
        "entry_count": len(list(path.iterdir())) if path.is_dir() else None,
        "uid": None if info is None else info.st_uid,
        "gid": None if info is None else info.st_gid,
        "mode": None if info is None else info.st_mode & 0o777,
        "mount_device": "synthetic",
        "device_identity": "synthetic-device",
        "free_bytes": usage.f_bavail * usage.f_frsize,
    }


def _ensure_host_path(
    resources: IntegrationResources, *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> dict[str, Any]:
    config = resources.config
    capability = resources.require_capability()
    path = config.identity.host_path
    if path.exists():
        raise ColdTablespaceIntegrationError("synthetic cold path unexpectedly already exists")
    _root_action(resources, action="create-cold-path", runner=runner)
    observed = _host_path(config)
    if (
        observed["exists"] is not True
        or observed["is_symlink"] is not False
        or observed["entry_count"] != 0
        or observed["uid"] != capability.runtime_uid
        or observed["gid"] != capability.runtime_gid
        or observed["mode"] != 0o700
    ):
        raise ColdTablespaceIntegrationError("root-created synthetic cold path differs from installer contract")
    return observed


def _remove_host_path(config: IntegrationConfig) -> bool:
    path = config.identity.host_path
    path.rmdir()
    return not path.exists()


def _docker_action(
    resources: IntegrationResources, *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> Callable[[tuple[str, ...]], Mapping[str, Any]]:
    config = resources.config

    def action(argv: tuple[str, ...]) -> Mapping[str, Any]:
        if not argv or argv[0] != config.docker_bin:
            raise ColdTablespaceIntegrationError("core Docker argv did not use trusted binary")
        resources.actions.append(argv)
        _checked(runner, argv, timeout=90)
        command = argv[1] if len(argv) > 1 else ""
        if command == "rename" and len(argv) >= 4:
            resources.known_containers.discard(argv[2])
            resources.known_containers.add(argv[3])
        elif command == "run":
            resources.known_containers.add(config.container_name)
        elif command == "rm" and len(argv) >= 4:
            resources.known_containers.discard(argv[-1])
        return {"returncode": 0}

    return action


def _cold_bind_refs(
    config: IntegrationConfig, *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = runner((config.docker_bin, "ps", "-a", "--format", "{{.Names}}"), timeout=30)
    if result.returncode != 0:
        raise ColdTablespaceIntegrationError("disposable container inventory failed")
    current: list[str] = []
    stopped: list[str] = []
    for name in (line.strip() for line in result.stdout.splitlines()):
        if not name:
            continue
        raw = inspect_container(config, name, runner=runner)
        running = bool(raw.get("State", {}).get("Running")) if isinstance(raw.get("State"), Mapping) else False
        for mount in raw.get("Mounts", []):
            if (
                isinstance(mount, Mapping)
                and mount.get("Source") == str(config.identity.host_path)
                and mount.get("Destination") == config.identity.container_path
            ):
                (current if running else stopped).append(name)
    return tuple(current), tuple(stopped)


def _target(
    config: IntegrationConfig,
    *,
    capability: RootEvidenceCapability,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    observed = _host_path(config)
    if observed["uid"] != capability.runtime_uid or observed["gid"] != capability.runtime_gid:
        raise ColdTablespaceIntegrationError(
            "cold tablespace host owner differs from the proven runtime identity"
        )
    _checked(
        runner,
        (
            config.docker_bin,
            "exec",
            "--user",
            f"{capability.runtime_uid}:{capability.runtime_gid}",
            config.container_name,
            "test",
            "-w",
            config.identity.container_path,
        ),
        timeout=30,
    )
    return {
        "container_name": config.container_name,
        "container_bind": str(config.identity.host_path),
        "host_path": str(config.identity.host_path),
        "device_identity": "synthetic-device",
        "host_mode": observed["mode"],
        "host_uid": observed["uid"],
        "host_gid": observed["gid"],
        "writable": True,
    }


def dependencies(
    resources: IntegrationResources,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
    health: Mapping[str, Any] | None = None,
    after_phase: Callable[[str], None] | None = None,
) -> InstallDependencies:
    config = resources.config

    def connection_factory() -> Any:
        return _PsycopgBoundary(connect(config))

    def pg_refs() -> tuple[str, ...]:
        connection = connection_factory()
        try:
            rows = connection.execute(
                "SELECT pg_tablespace_location(oid) AS target FROM pg_tablespace "
                "WHERE pg_tablespace_location(oid) <> ''"
            )
            return tuple(str(row["target"]) for row in rows if row.get("target") == config.identity.container_path)
        finally:
            connection.close()

    def catalog_dependents() -> int:
        connection = connection_factory()
        try:
            rows = connection.execute(
                "SELECT count(*)::bigint AS count FROM pg_depend AS d "
                "JOIN pg_tablespace AS s ON s.oid = d.refobjid WHERE s.spcname = %s",
                (config.identity.tablespace,),
            )
            return int(rows[0]["count"]) if rows else 1
        finally:
            connection.close()

    return InstallDependencies(
        inspect_path=lambda: _host_path(config),
        inspect_health=lambda: {
            key: value
            for key, value in (health or {"healthy": False, "blockers": ["root evidence was not prepared"]}).items()
            if key != "backup"
        },
        inspect_backup=lambda _targets=(): dict(
            (health or {}).get("backup") or {"complete": False, "blockers": ["root backup evidence was not prepared"]}
        ),
        inspect_container=lambda: inspect_container(config, config.container_name, runner=runner),
        inspect_named_container=lambda name: inspect_container(config, name, runner=runner),
        docker=_docker_action(resources, runner=runner),
        connect=connection_factory,
        connect_readonly=connection_factory,
        inspect_target=lambda: _target(
            config, capability=resources.require_capability(), runner=runner
        ),
        current_bind_references=lambda: _cold_bind_refs(config, runner=runner)[0],
        stopped_bind_references=lambda: _cold_bind_refs(config, runner=runner)[1],
        pg_tblspc_references=pg_refs,
        catalog_dependents=catalog_dependents,
        inspect_host_path_for_rollback=lambda: _host_path(config),
        ensure_host_path=lambda: _ensure_host_path(resources, runner=runner),
        remove_host_path=lambda: _remove_host_path(config),
        wait_ready=lambda: wait_for_sql(config),
        inspect_quiescence=lambda: {
            "units": {
                unit: {"active_state": "inactive", "sub_state": "dead", "result": "success"} for unit in _WRITER_UNITS
            }
        },
        now=lambda: datetime.now(UTC),
        after_phase=after_phase,
    )


class _PsycopgBoundary:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[object] = ()) -> list[Mapping[str, Any]]:
        return execute(self._connection, sql, params)

    def close(self) -> None:
        self._connection.close()


_WRITER_UNITS = (
    "nhms-node27-autopipe.service",
    "nhms-node27-autopipe.timer",
    "nhms-node27-timeseries-compression.service",
    "nhms-node27-timeseries-compression.timer",
    "nhms-node27-timeseries-retention.service",
    "nhms-node27-timeseries-retention.timer",
)


def assert_new_chunk_pg_default(config: IntegrationConfig) -> None:
    """Prove fresh origin/sibling/index/TOAST placement remains ``pg_default``."""

    connection = connect(config)
    try:
        for schema, table in (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries")):
            execute(connection, f"INSERT INTO {schema}.{table} VALUES (2, '2026-02-01T00:00:00Z', 3.0)")
            chunks = execute(
                connection,
                "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                "WHERE hypertable_schema = %s AND hypertable_name = %s "
                "AND range_start <= '2026-02-01T00:00:00Z'::timestamptz "
                "AND range_end > '2026-02-01T00:00:00Z'::timestamptz",
                (schema, table),
            )
            if len(chunks) != 1:
                raise ColdTablespaceIntegrationError("new synthetic chunk identity is not singular")
            chunk = chunks[0]
            regclass = f"{chunk['chunk_schema']}.{chunk['chunk_name']}"
            execute(connection, "SELECT compress_chunk(%s::regclass)", (regclass,))
            rows = execute(
                connection,
                "WITH origin AS ("
                " SELECT c.oid FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                " WHERE n.nspname = %s AND c.relname = %s"
                "), sibling AS ("
                " SELECT c.oid FROM _timescaledb_catalog.chunk AS source "
                " JOIN _timescaledb_catalog.chunk AS compressed ON compressed.id = source.compressed_chunk_id "
                " JOIN pg_class AS c ON c.relname = compressed.table_name "
                " JOIN pg_namespace AS n ON n.oid = c.relnamespace AND n.nspname = compressed.schema_name "
                " WHERE source.schema_name = %s AND source.table_name = %s"
                "), heaps AS (SELECT oid FROM origin UNION SELECT oid FROM sibling), "
                "toast AS (SELECT reltoastrelid AS oid FROM pg_class "
                "WHERE oid IN (SELECT oid FROM heaps) AND reltoastrelid <> 0), "
                "members AS ("
                " SELECT oid FROM heaps UNION SELECT indexrelid FROM pg_index "
                "WHERE indrelid IN (SELECT oid FROM heaps) "
                " UNION SELECT oid FROM toast UNION SELECT indexrelid FROM pg_index "
                "WHERE indrelid IN (SELECT oid FROM toast)"
                ") SELECT COALESCE(space.spcname, 'pg_default') AS tablespace FROM pg_class AS relation "
                " LEFT JOIN pg_tablespace AS space ON space.oid = relation.reltablespace "
                " WHERE relation.oid IN (SELECT oid FROM members)",
                (chunk["chunk_schema"], chunk["chunk_name"], chunk["chunk_schema"], chunk["chunk_name"]),
            )
            if len(rows) < 2 or any(row["tablespace"] != "pg_default" for row in rows):
                raise ColdTablespaceIntegrationError("new origin/sibling/index/TOAST member is not in pg_default")
    finally:
        connection.close()


def cleanup(
    resources: IntegrationResources, *, runner: Callable[..., subprocess.CompletedProcess[str]] = _run
) -> Mapping[str, bool]:
    config = resources.config
    validate_isolated_config(config)
    failures: list[str] = []
    containers_absent = True
    for name in (config.container_name, config.prior_container_name):
        result = runner((config.docker_bin, "rm", "-f", name), timeout=60)
        if result.returncode not in {0, 1}:
            containers_absent = False
            failures.append(f"docker rm failed for {name}")
        inspected = runner((config.docker_bin, "inspect", name), timeout=20)
        if inspected.returncode == 0:
            containers_absent = False
            failures.append(f"container remains after cleanup: {name}")
    port_free = True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", config.host_port))
    except OSError:
        port_free = False
        failures.append("disposable PostgreSQL port remains bound")
    root = config.work_root
    helpers_absent = False
    try:
        assert_root_helpers_absent(config.identity, runner=runner)
    except RootCapabilityError:
        failures.append("owned root helper remains after cleanup")
    else:
        helpers_absent = True
    if root.exists():
        try:
            children = {child.name for child in root.iterdir()}
        except OSError:
            failures.append("owned disposable work root cannot be inventoried")
        else:
            unknown = children - KNOWN_WORK_ROOT_CHILDREN
            if unknown:
                failures.append("owned disposable work root has unknown children")
            elif containers_absent and port_free and helpers_absent:
                try:
                    _root_action(resources, action="cleanup", runner=runner)
                except ColdTablespaceIntegrationError:
                    failures.append("owned disposable work root removal failed")
                else:
                    try:
                        root.rmdir()
                    except OSError:
                        failures.append("owned disposable work root was not empty after cleanup")
    if root.exists():
        failures.append("owned disposable work root remains")
    if failures:
        raise ColdTablespaceIntegrationError("; ".join(failures))
    return {"container_absent": True, "work_root_absent": True}
