#!/usr/bin/env python
"""Dry-run-default installer for node-27's fixed ``nhms_cold`` tablespace.

The CLI performs only local observations by default.  ``--enforce`` is an
explicit operator action and is still fail-closed unless every descriptor-bound
storage, container, catalog, backup, ownership and capacity gate succeeds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.node27_cold_tablespace_evidence import EvidencePolicy
from packages.common.node27_cold_tablespace_host import (
    ColdHostError,
    DockerBoundary,
    EvidencePaths,
    SystemdBoundary,
    create_fresh_host_path,
    inspect_host_path,
    inspect_running_target,
    inspect_storage_evidence,
    inspect_storage_health,
    remove_installer_owned_host_path,
)
from packages.common.node27_cold_tablespace_identity import PRODUCTION_IDENTITY, ColdTablespaceIdentity
from packages.common.node27_cold_tablespace_install import (
    WRITER_TIMER_UNITS,
    InstallConfig,
    InstallDependencies,
    run_install,
)
from packages.common.redaction import redact_database_dsn, redact_text

_CONNECT_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT = "20s"


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _mode(value: str) -> int:
    parsed = int(value, 8)
    if parsed < 0 or parsed > 0o777:
        raise argparse.ArgumentTypeError("must be an octal permission mode")
    return parsed


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return path


def _smart_path(value: str) -> tuple[str, Path]:
    device, separator, raw_path = value.partition("=")
    if not separator or not device.startswith("/dev/") or not raw_path:
        raise argparse.ArgumentTypeError("must be DEVICE=ABSOLUTE_PATH")
    return device, _absolute_path(raw_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enforce", action="store_true", help="Allow the narrowly gated container/catalog mutation.")
    parser.add_argument("--receipt-path", type=_absolute_path, required=True)
    parser.add_argument("--recovery-path", type=_absolute_path, required=True)
    parser.add_argument("--head-sha")
    parser.add_argument("--expected-uid", type=int, required=True)
    parser.add_argument("--expected-gid", type=int, required=True)
    parser.add_argument("--expected-mode", type=_mode, required=True)
    parser.add_argument("--expected-device-identity", required=True)
    parser.add_argument("--install-required-bytes", type=_positive, required=True)
    parser.add_argument("--rollback-headroom-bytes", type=_positive, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--evidence-hostname", default=os.getenv("NODE27_COLD_EVIDENCE_HOSTNAME"))
    parser.add_argument("--array-device", default=os.getenv("NODE27_COLD_ARRAY_DEVICE", "/dev/md0"))
    parser.add_argument("--evidence-max-age-seconds", type=_positive, default=None)
    parser.add_argument("--evidence-owner-uid", type=int, default=0)
    parser.add_argument("--evidence-approved-mode", type=_mode, action="append", default=[])
    parser.add_argument("--mdadm-evidence", type=_absolute_path)
    parser.add_argument("--smart-evidence", type=_smart_path, action="append", default=[])
    parser.add_argument("--backup-evidence", type=_absolute_path)
    parser.add_argument("--pgdata-path", default=os.getenv("NODE27_COLD_PGDATA_PATH", "/home/nwm/nhms-pgdata"))
    parser.add_argument("--mdadm-bin", default="/usr/sbin/mdadm")
    parser.add_argument("--smartctl-bin", default="/usr/sbin/smartctl")
    parser.add_argument("--backup-inventory-bin", default="/usr/local/sbin/nhms-backup-inventory")
    return parser


def config_from_args(args: argparse.Namespace) -> InstallConfig:
    if not args.receipt_path.is_absolute() or not args.recovery_path.is_absolute():
        raise ValueError("receipt and recovery paths must be absolute")
    if args.expected_uid < 0 or args.expected_gid < 0:
        raise ValueError("expected uid/gid must be non-negative")
    if not args.expected_device_identity:
        raise ValueError("expected device identity is required")
    return InstallConfig(
        enforce=args.enforce,
        receipt_path=args.receipt_path,
        recovery_path=args.recovery_path,
        head_sha=args.head_sha,
        expected_uid=args.expected_uid,
        expected_gid=args.expected_gid,
        expected_mode=args.expected_mode,
        expected_device_identity=args.expected_device_identity,
        install_required_bytes=args.install_required_bytes,
        rollback_headroom_bytes=args.rollback_headroom_bytes,
        identity=PRODUCTION_IDENTITY,
    )


def _evidence_policy(args: argparse.Namespace) -> EvidencePolicy:
    if not args.evidence_hostname:
        raise ValueError("evidence hostname is required")
    if args.evidence_max_age_seconds is None:
        raise ValueError("positive evidence maximum age is required")
    if not args.evidence_approved_mode:
        raise ValueError("at least one approved evidence mode is required")
    if not args.pgdata_path.startswith("/"):
        raise ValueError("PGDATA path must be absolute")
    return EvidencePolicy(
        expected_hostname=args.evidence_hostname,
        array_device=args.array_device,
        max_age_seconds=args.evidence_max_age_seconds,
        expected_uid=args.evidence_owner_uid,
        approved_modes=tuple(args.evidence_approved_mode),
        mdadm_argv=(args.mdadm_bin, "--detail", args.array_device),
        smartctl_prefix=(args.smartctl_bin,),
        backup_argv=(args.backup_inventory_bin, "--json"),
        expected_pgdata=args.pgdata_path,
    )


def _evidence_paths(args: argparse.Namespace) -> EvidencePaths:
    if args.mdadm_evidence is None or args.backup_evidence is None:
        raise ValueError("mdadm and backup evidence paths are required")
    smart = dict(args.smart_evidence)
    if len(smart) != len(args.smart_evidence):
        raise ValueError("SMART evidence devices must be unique")
    if not smart:
        raise ValueError("SMART evidence paths are required")
    return EvidencePaths(mdadm=args.mdadm_evidence, smart=smart, backup=args.backup_evidence)


def _connect(database_url: str | None) -> Any:
    if not database_url:
        raise RuntimeError("database URL is required for enforce")
    try:
        import psycopg2
        import psycopg2.extras

        connection = psycopg2.connect(
            database_url,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            application_name="nhms-cold-tablespace-install",
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        connection.autocommit = True
        return _PsycopgConnection(connection)
    except Exception as error:
        raise RuntimeError(redact_database_dsn("database connection failed", database_url)) from error


class _PsycopgConnection:
    """Minimal parameterized query seam; never serializes the DSN or cursor."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[object] = ()) -> list[Mapping[str, Any]]:
        with self._connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = '{_STATEMENT_TIMEOUT}'")
            cursor.execute(sql, tuple(params))
            if cursor.description is None:
                return []
            return [dict(row) for row in cursor.fetchall()]

    def close(self) -> None:
        self._connection.close()


def _cold_bind_references(docker: DockerBoundary) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return docker.current_and_stopped_cold_binds()


def _pg_tblspc_references(
    connection_factory: Callable[[], Any], *, identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY
) -> Sequence[str]:
    connection = connection_factory()
    try:
        rows = connection.execute(
            "SELECT pg_tablespace_location(oid) AS target FROM pg_tablespace "
            "WHERE pg_tablespace_location(oid) <> '' ORDER BY target"
        )
        return tuple(str(row["target"]) for row in rows if row.get("target") == identity.container_path)
    finally:
        connection.close()


def _catalog_dependents(
    connection_factory: Callable[[], Any], *, identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY
) -> int:
    connection = connection_factory()
    try:
        rows = connection.execute(
            "SELECT count(*)::bigint AS count FROM pg_depend AS d "
            "JOIN pg_tablespace AS s ON s.oid = d.refobjid WHERE s.spcname = %s",
            (identity.tablespace,),
        )
        return int(rows[0]["count"]) if rows else 1
    finally:
        connection.close()


def dependencies_from_args(args: argparse.Namespace, config: InstallConfig) -> InstallDependencies:
    docker = DockerBoundary(identity=config.identity)
    systemd = SystemdBoundary()
    policy = _evidence_policy(args)
    paths = _evidence_paths(args)
    cached_health: dict[str, Any] | None = None

    def inspect_health() -> Mapping[str, Any]:
        nonlocal cached_health
        if cached_health is None:
            # Health does not depend on catalog scope. Parse it once rather than
            # requiring a fictional empty backup inventory during dry-run.
            cached_health = inspect_storage_health(paths, policy=policy, now=datetime.now(UTC))
        return cached_health

    def inspect_backup(targets: tuple[str, ...] = ()) -> Mapping[str, Any]:
        # Backup evidence is deliberately re-bound to the just-discovered
        # external pg_tblspc target set on enforce.
        _health, backup = inspect_storage_evidence(
            paths,
            policy=policy,
            external_targets=targets,
            now=datetime.now(UTC),
        )
        return backup

    def connect() -> _PsycopgConnection:
        return _connect(args.database_url)

    def inspect_target() -> Mapping[str, Any]:
        return inspect_running_target(docker)

    def ensure_host_path() -> Mapping[str, Any]:
        return create_fresh_host_path(
            expected_uid=config.expected_uid,
            expected_gid=config.expected_gid,
            expected_mode=config.expected_mode,
            expected_device_identity=config.expected_device_identity,
            identity=config.identity,
        )

    def remove_host_path() -> bool:
        return remove_installer_owned_host_path(
            expected_device_identity=config.expected_device_identity,
            expected_uid=config.expected_uid,
            expected_gid=config.expected_gid,
            expected_mode=config.expected_mode,
            identity=config.identity,
        )

    def wait_ready() -> None:
        connection = connect()
        try:
            rows = connection.execute("SELECT 1 AS live")
            if rows != [{"live": 1}]:
                raise RuntimeError("restored container SQL read path is unavailable")
        finally:
            connection.close()

    return InstallDependencies(
        inspect_path=lambda: inspect_host_path(identity=config.identity),
        inspect_health=inspect_health,
        inspect_backup=inspect_backup,
        inspect_container=lambda: docker.inspect(config.identity.container_name),
        inspect_named_container=docker.inspect,
        docker=docker.action,
        connect=connect,
        connect_readonly=connect,
        inspect_target=inspect_target,
        current_bind_references=lambda: _cold_bind_references(docker)[0],
        stopped_bind_references=lambda: _cold_bind_references(docker)[1],
        pg_tblspc_references=lambda: _pg_tblspc_references(connect, identity=config.identity),
        catalog_dependents=lambda: _catalog_dependents(connect, identity=config.identity),
        inspect_host_path_for_rollback=lambda: inspect_host_path(identity=config.identity),
        ensure_host_path=ensure_host_path,
        remove_host_path=remove_host_path,
        wait_ready=wait_ready,
        inspect_quiescence=lambda: systemd.inspect_quiescence(WRITER_TIMER_UNITS),
        now=lambda: datetime.now(UTC),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = config_from_args(args)
        dependencies = dependencies_from_args(args, config)
        result = run_install(config, dependencies)
    except (ColdHostError, RuntimeError, ValueError, argparse.ArgumentTypeError) as error:
        print(json.dumps({"outcome": "no_go", "reason": redact_text(str(error))}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result.receipt, sort_keys=True))
    return 0 if result.outcome in {"dry_run", "already_ready", "installed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
