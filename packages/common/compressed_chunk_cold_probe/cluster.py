"""Disposable-cluster identity, docker, SQL session, and cleanup helpers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from packages.common.compressed_chunk_cold_probe.types import (
    CONTAINER_COLD,
    CONTAINER_FULL,
    CONTAINER_PGDATA,
    OWNED_NAME_RE,
    PROBE_NAME_PREFIX,
    OwnedResources,
    ProbeConfig,
    ProbeError,
)
from packages.common.compressed_chunk_cold_residency import (
    LIVE_CONTAINER_NAME,
    LIVE_PORT,
    PINNED_IMAGE_ID,
    PINNED_IMAGE_REF,
    ColdResidencyError,
    check_engine_identity,
    refuse_live_identity,
    validate_catalog_path,
)


def config_from_args(args: argparse.Namespace) -> ProbeConfig:
    token = uuid.uuid4().hex[:12]
    container_name = args.container_name.strip() or f"{PROBE_NAME_PREFIX}{token}"
    work_root_raw = args.work_root.strip() or f"/tmp/{PROBE_NAME_PREFIX}{token}"
    output_raw = str(args.output).strip()
    extra = [work_root_raw, f"{work_root_raw.rstrip('/')}/pgdata"]
    if output_raw:
        extra.append(str(Path(output_raw).parent))
    refuse_live_identity(
        container_name=container_name,
        host_port=int(args.host_port),
        pgdata=f"{work_root_raw.rstrip('/')}/pgdata",
        extra_paths=extra,
    )
    work_root = Path(work_root_raw).resolve()
    output_path = Path(output_raw).resolve() if output_raw else None
    extra_resolved = [str(work_root)]
    if output_path is not None:
        extra_resolved.append(str(output_path.parent))
    refuse_live_identity(
        container_name=container_name,
        host_port=int(args.host_port),
        pgdata=str(work_root / "pgdata"),
        extra_paths=extra_resolved,
    )
    if args.live_container_name == container_name or int(args.host_port) == LIVE_PORT:
        raise ColdResidencyError("refusing live cluster identity")
    if str(args.live_pgdata) == str(work_root / "pgdata"):
        raise ColdResidencyError("refusing live PGDATA path")
    if not OWNED_NAME_RE.fullmatch(container_name):
        raise ColdResidencyError(f"container name must match {OWNED_NAME_RE.pattern}: {container_name}")
    if PROBE_NAME_PREFIX not in work_root.name:
        raise ColdResidencyError(f"work root must be identity-bound with {PROBE_NAME_PREFIX}")
    return ProbeConfig(
        mode=str(args.mode),
        container_name=container_name,
        host_port=int(args.host_port),
        work_root=work_root,
        image_id=str(args.image_id).strip() or PINNED_IMAGE_ID,
        image_ref=str(args.image_ref).strip() or PINNED_IMAGE_REF,
        lock_timeout=str(args.lock_timeout),
        statement_timeout=str(args.statement_timeout),
        docker_bin=str(args.docker_bin),
        output_path=output_path,
        keep=bool(args.keep),
    )


def _run(argv: Sequence[str], *, timeout: int = 60, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=False, capture_output=True, text=True, input=stdin, timeout=timeout)


def inspect_live_image(
    docker_bin: str,
    container: str = LIVE_CONTAINER_NAME,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> dict[str, Any]:
    result = runner([docker_bin, "inspect", "--format", "{{.Config.Image}}|{{.Image}}", container], timeout=20)
    if result.returncode != 0:
        raise ProbeError("live image inspect failed; refusing to overwrite the committed pin")
    config_image, _, image_id = result.stdout.strip().partition("|")
    config_image = config_image.strip()
    image_id = image_id.strip()
    if not image_id or not config_image:
        raise ProbeError("live image inspect returned empty identity; refusing pin overwrite")
    image_meta = runner(
        [docker_bin, "inspect", "--format", "{{json .RepoTags}}|{{json .RepoDigests}}", image_id],
        timeout=20,
    )
    repo_tags: list[str] = []
    repo_digests: list[str] = []
    if image_meta.returncode == 0 and image_meta.stdout.strip():
        tags_raw, _, digests_raw = image_meta.stdout.strip().partition("|")
        try:
            parsed_tags = json.loads(tags_raw)
            parsed_digests = json.loads(digests_raw)
            if isinstance(parsed_tags, list):
                repo_tags = [str(item) for item in parsed_tags]
            if isinstance(parsed_digests, list):
                repo_digests = [str(item) for item in parsed_digests]
        except json.JSONDecodeError as error:
            raise ProbeError("live image RepoTags/RepoDigests inspect is not JSON") from error
    alias = None
    if config_image == image_id:
        alias = "digest_image_id"
    return {
        "image_ref": config_image,
        "image_id": image_id,
        "config_image": config_image,
        "live_ref_alias": alias,
        "repo_tags": repo_tags,
        "repo_digests": repo_digests,
    }


def assert_engine_identity(
    *,
    requested_image_id: str,
    requested_image_ref: str,
    live_image_id: str,
    live_image_ref: str,
    used_image_id: str,
    used_image_ref: str,
    server_version: str | None = None,
    timescaledb_version: str | None = None,
) -> dict[str, bool]:
    try:
        return check_engine_identity(
            live_image_id=live_image_id,
            live_image_ref=live_image_ref,
            requested_image_id=requested_image_id,
            requested_image_ref=requested_image_ref,
            used_image_id=used_image_id,
            used_image_ref=used_image_ref,
            server_version=server_version,
            timescaledb_version=timescaledb_version,
        )
    except ColdResidencyError as error:
        raise ProbeError(f"engine identity: {error}") from error


def validate_catalog_path_preflight(
    *,
    catalog_location: str | None,
    expected_location: str,
) -> dict[str, Any]:
    if catalog_location is None or str(catalog_location).strip() == "":
        return {
            "ok": False,
            "refused": True,
            "catalog_location": catalog_location,
            "expected_location": expected_location,
            "error": "catalog location is missing",
        }
    try:
        validate_catalog_path(catalog_location=str(catalog_location), expected_location=expected_location)
    except ColdResidencyError as error:
        return {
            "ok": False,
            "refused": True,
            "catalog_location": catalog_location,
            "expected_location": expected_location,
            "error": str(error),
        }
    return {
        "ok": True,
        "refused": False,
        "catalog_location": catalog_location,
        "expected_location": expected_location,
    }


def prepare_work_root(config: ProbeConfig) -> OwnedResources:
    owned = OwnedResources(container_name=config.container_name, work_root=config.work_root)
    if config.work_root.exists():
        raise ProbeError(f"work root already exists: {config.work_root}")
    config.work_root.mkdir(parents=True, mode=0o700)
    owned.created_work_root = True
    created = []
    for name in ("pgdata", "cold", "full"):
        path = config.work_root / name
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        created.append(path)
    owned.created_paths = tuple(created)
    return owned


def _host_user_spec() -> str:
    return f"{os.getuid()}:{os.getgid()}"


def _container_logs(docker_bin: str, container: str) -> str:
    result = _run([docker_bin, "logs", "--tail", "80", container], timeout=20)
    return ((result.stdout or "") + (result.stderr or ""))[-4000:]


def docker_run_argv(config: ProbeConfig) -> list[str]:
    env_file = config.work_root / "postgres.env"
    env_file.write_text(
        "POSTGRES_USER=postgres\n"
        f"POSTGRES_PASSWORD={config.password}\n"
        "POSTGRES_DB=postgres\n"
        "PGDATA=/home/postgres/pgdata/data\n"
        "TIMESCALEDB_TELEMETRY=off\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return [
        config.docker_bin,
        "run",
        "-d",
        "--name",
        config.container_name,
        "--user",
        _host_user_spec(),
        "--env-file",
        str(env_file),
        "-p",
        f"127.0.0.1:{config.host_port}:5432",
        "-v",
        f"{config.work_root / 'pgdata'}:{CONTAINER_PGDATA}",
        "-v",
        f"{config.work_root / 'cold'}:{CONTAINER_COLD}",
        "--tmpfs",
        f"{CONTAINER_FULL}:size=1048576,uid={os.getuid()},gid={os.getgid()},mode=0700",
        config.image_id,
        "postgres",
        "-c",
        "shared_preload_libraries=timescaledb",
    ]


def wait_for_port(host: str, port: int, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.25)
    raise ProbeError(f"disposable cluster did not accept connections on {host}:{port}")


def connect(config: ProbeConfig, *, autocommit: bool = True) -> Any:
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
        application_name="nhms-1892-probe",
    )
    connection.autocommit = autocommit
    return connection


def wait_for_sql(config: ProbeConfig, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            connection = connect(config)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                return
            finally:
                connection.close()
        except Exception as error:  # noqa: BLE001
            last = type(error).__name__
            time.sleep(0.5)
    raise ProbeError(f"disposable PostgreSQL did not become ready ({last})")


def execute(connection: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        if cursor.description is None:
            return []
        converted: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            converted.append(
                dict(row)
                if isinstance(row, dict)
                else dict(zip((c.name for c in cursor.description), row, strict=False))
            )
        return converted


def scalar(connection: Any, sql: str, params: Any = None) -> Any:
    rows = execute(connection, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def cleanup_owned(
    owned: OwnedResources,
    *,
    docker_bin: str,
    keep: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "kept": keep,
        "identity_bound": owned.identity_bound(),
        "container_removed": False,
        "container_absent": False,
        "work_root_removed": False,
        "work_root_absent": False,
        "refused": None,
    }
    if keep:
        proof["refused"] = "keep requested"
        return proof
    if not owned.identity_bound():
        proof["refused"] = "unowned identity"
        return proof
    if owned.container_name:
        runner([docker_bin, "rm", "-f", owned.container_name], timeout=60)
        inspect = runner([docker_bin, "inspect", "-f", "{{.Name}}", owned.container_name], timeout=20)
        proof["container_removed"] = inspect.returncode != 0
        proof["container_absent"] = inspect.returncode != 0
    if owned.work_root is not None and owned.created_work_root:
        shutil.rmtree(owned.work_root, ignore_errors=True)
        proof["work_root_removed"] = not owned.work_root.exists()
        proof["work_root_absent"] = not owned.work_root.exists()
    return proof
