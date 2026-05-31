from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    unlink_no_follow,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "two-node-e2e"
APPROVED_EVIDENCE_ROOTS = (REPO_ROOT / "artifacts", Path("/scratch/frd_muziyao"))
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_DDL_SUFFIX_RE = re.compile(r"[^a-z0-9_]+")

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
LIVE_EVIDENCE_SCHEMA = "nhms.readonly_db_boundary.evidence.v1"
SIMULATED_EVIDENCE_SCHEMA = "nhms.readonly_db_boundary.evidence.simulated.v1"
AUTHORITATIVE_EVIDENCE_FILENAMES = (
    "summary.json",
    "role.json",
    "route_smoke.json",
    "permission_probes.json",
)
READONLY_DB_URL_ENVS = (
    "NHMS_DISPLAY_READONLY_DATABASE_URL",
    "NHMS_READONLY_DB_VALIDATION_DATABASE_URL",
)
VALIDATION_ENV_PREFIX = "NHMS_READONLY_DB_VALIDATION_"
VALIDATION_CONNECT_TIMEOUT_SECONDS = 5
VALIDATION_STATEMENT_TIMEOUT_MS = 10_000
VALIDATION_LOCK_TIMEOUT_MS = 2_000
VALIDATION_IDLE_TIMEOUT_MS = 10_000
DENIED_SQLSTATES = frozenset({"25006", "42501"})
BLOCKED_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
ROLE_ATTRIBUTE_WRITE_FLAGS = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
)
TABLE_CATALOG_MUTATING_OPERATIONS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
TABLE_CATALOG_ONLY_MUTATING_OPERATIONS = ("TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")
ROUTE_FIXTURE_BLOCKER_ERROR_CODES = frozenset(
    {
        "QHH_LATEST_PRODUCT_UNAVAILABLE",
        "PIPELINE_CYCLE_NOT_FOUND",
        "PIPELINE_STRICT_IDENTITY_NOT_FOUND",
        "JOB_NOT_FOUND",
        "JOB_LOG_NOT_PUBLISHED",
        "JOB_LOG_NOT_FOUND",
        "JOB_LOG_URI_UNSUPPORTED",
        "JOB_LOG_ACCESS_DENIED",
    }
)


class ReadonlyDbValidationError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class ProbeTarget:
    schema: str
    table: str
    surface: str

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass(frozen=True)
class PermissionProbeSpec:
    operation: str
    target: ProbeTarget | None
    command: str
    probe_column: str | None = None
    ddl_schema: str | None = None
    ddl_table: str | None = None


@dataclass(frozen=True)
class ProbeExecution:
    outcome: str
    sqlstate: str | None = None
    message: str | None = None
    rowcount: int | None = None
    rolled_back: bool = True


@dataclass(frozen=True)
class RouteHttpResponse:
    status_code: int
    body: Any | None = None
    text: str = ""


@dataclass(frozen=True)
class ReadonlyDbMergeSourceEvidence:
    source_dir: Path
    summary: dict[str, Any]
    artifacts: dict[str, dict[str, Any]]


class ReadonlyDbProbeAdapter(Protocol):
    def current_role(self) -> dict[str, Any]:
        ...

    def discover_display_identity(self) -> dict[str, Any]:
        ...

    def schema_exists(self, schema: str) -> bool:
        ...

    def table_exists(self, target: ProbeTarget) -> bool:
        ...

    def table_privileges(self, target: ProbeTarget) -> dict[str, bool]:
        ...

    def column_privileges(self, target: ProbeTarget) -> dict[str, list[str]]:
        ...

    def sequence_privileges(self, target: ProbeTarget) -> list[dict[str, Any]]:
        ...

    def schema_privileges(self, schema: str) -> dict[str, bool]:
        ...

    def database_privileges(self) -> dict[str, Any]:
        ...

    def audited_schema_sequence_privileges(self, schemas: tuple[str, ...]) -> list[dict[str, Any]]:
        ...

    def reachable_role_privileges(
        self,
        targets: tuple[ProbeTarget, ...],
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        ...

    def first_updatable_column(self, target: ProbeTarget) -> str | None:
        ...

    def execute_probe(self, spec: PermissionProbeSpec) -> ProbeExecution:
        ...


RouteRequester = Callable[[str, str], RouteHttpResponse]

PERMISSION_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    ProbeTarget("hydro", "hydro_run", "hydro_run_terminal_state"),
    ProbeTarget("hydro", "river_timeseries", "hydro_display_timeseries"),
    ProbeTarget("met", "forecast_cycle", "met_cycle_state"),
    ProbeTarget("met", "forcing_station_timeseries", "met_station_timeseries"),
    ProbeTarget("ops", "pipeline_job", "pipeline_job_state"),
    ProbeTarget("ops", "pipeline_event", "pipeline_event_audit"),
)


@dataclass(frozen=True)
class ReadonlyDbValidationConfig:
    evidence_root: Path
    run_id: str
    database_url: str | None = None
    source: str | None = None
    cycle_time: str | None = None
    strict_run_id: str | None = None
    model_id: str | None = None
    job_id: str | None = None
    force: bool = False

    @property
    def lane_dir(self) -> Path:
        return self.evidence_root / self.run_id / "db" / "readonly-db-boundary"

    @classmethod
    def from_env(
        cls,
        *,
        evidence_root: Path | None = None,
        run_id: str | None = None,
        database_url: str | None = None,
        source: str | None = None,
        cycle_time: str | None = None,
        strict_run_id: str | None = None,
        model_id: str | None = None,
        job_id: str | None = None,
        force: bool = False,
    ) -> ReadonlyDbValidationConfig:
        selected_database_url = database_url or _first_env(READONLY_DB_URL_ENVS)
        selected_evidence_root = evidence_root or _path_env(
            "NHMS_READONLY_DB_VALIDATION_EVIDENCE_ROOT",
            DEFAULT_EVIDENCE_ROOT,
        )
        return cls(
            evidence_root=_safe_resolved_evidence_root(selected_evidence_root),
            run_id=_safe_run_id(
                run_id or os.getenv("NHMS_READONLY_DB_VALIDATION_EVIDENCE_RUN_ID") or _default_run_id()
            ),
            database_url=selected_database_url.strip() if selected_database_url else None,
            source=source or os.getenv(f"{VALIDATION_ENV_PREFIX}SOURCE") or None,
            cycle_time=cycle_time or os.getenv(f"{VALIDATION_ENV_PREFIX}CYCLE_TIME") or None,
            strict_run_id=strict_run_id or os.getenv(f"{VALIDATION_ENV_PREFIX}RUN_ID") or None,
            model_id=model_id or os.getenv(f"{VALIDATION_ENV_PREFIX}MODEL_ID") or None,
            job_id=job_id or os.getenv(f"{VALIDATION_ENV_PREFIX}JOB_ID") or None,
            force=force,
        )


@dataclass
class EvidenceWriter:
    evidence_root: Path
    lane_dir: Path
    force: bool = False
    _created_paths: set[Path] = field(default_factory=set)

    def prepare(self) -> None:
        evidence_root = _safe_resolved_evidence_root(self.evidence_root)
        lane_dir = self.lane_dir.resolve(strict=False)
        try:
            lane_dir.relative_to(evidence_root)
        except ValueError as error:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                "Evidence lane directory must stay under the approved evidence root.",
            ) from error
        _refuse_symlink_components(evidence_root)
        _refuse_symlink_components(lane_dir.parent)
        if lane_dir.exists() and lane_dir.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence lane path must not be a symlink: {lane_dir}.",
            )
        if lane_dir.exists() and not lane_dir.is_dir():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence lane path must be a directory: {lane_dir}.",
            )
        if lane_dir.exists() and any(lane_dir.iterdir()) and not self.force:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_EXISTS",
                f"Evidence bundle already exists: {lane_dir}. Use --force to overwrite this run_id.",
            )
        try:
            ensure_directory_no_follow(evidence_root)
            ensure_directory_no_follow(lane_dir, containment_root=evidence_root)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to prepare evidence directory: {error}") from error

    def write_json(self, path: Path, payload: Any) -> None:
        safe_path = self._safe_file_path(path)
        if safe_path.exists() and safe_path not in self._created_paths and not self.force:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_EXISTS",
                f"Evidence file already exists: {safe_path}. Use --force to overwrite this run_id.",
            )
        content = json.dumps(redact_payload(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        try:
            atomic_write_bytes_no_follow(safe_path, content, containment_root=self.lane_dir)
            self._created_paths.add(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to write evidence file: {error}") from error

    def remove_json(self, path: Path) -> None:
        safe_path = self._safe_file_path(path)
        try:
            unlink_no_follow(safe_path, containment_root=self.lane_dir, missing_ok=True)
            self._created_paths.discard(safe_path)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to remove stale evidence file: {error}") from error

    def _safe_file_path(self, path: Path) -> Path:
        if path.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence file must not be a symlink: {path}.",
            )
        resolved_lane = self.lane_dir.resolve(strict=False)
        resolved_parent = path.parent.resolve(strict=False)
        try:
            resolved_parent.relative_to(resolved_lane)
        except ValueError as error:
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                "Evidence file path must stay under the readonly DB evidence lane.",
            ) from error
        _refuse_symlink_components(path.parent)
        try:
            ensure_directory_no_follow(path.parent, containment_root=self.lane_dir)
        except SafeFilesystemError as error:
            error_code = (
                "READONLY_DB_EVIDENCE_WRITE_FAILED"
                if error.kind == "io"
                else "READONLY_DB_EVIDENCE_PATH_UNSAFE"
            )
            raise ReadonlyDbValidationError(error_code, f"Failed to prepare evidence file parent: {error}") from error
        return resolved_parent / path.name


class PsycopgReadonlyDbProbeAdapter:
    def __init__(self, database_url: str, *, ddl_suffix: str) -> None:
        self.database_url = database_url
        self.ddl_suffix = ddl_suffix

    def current_role(self) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        current_user,
                        session_user,
                        current_setting('transaction_read_only') AS transaction_read_only
                    """
                )
                session = dict(cursor.fetchone())
                cursor.execute(
                    """
                    SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolreplication, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
                role = dict(cursor.fetchone() or {})
        return {**session, **role}

    def discover_display_identity(self) -> dict[str, Any]:
        identity: dict[str, Any] = {}
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT run_id, source_id AS source, cycle_time, model_id
                        FROM hydro.hydro_run
                        WHERE source_id IS NOT NULL
                          AND cycle_time IS NOT NULL
                          AND model_id IS NOT NULL
                        ORDER BY updated_at DESC NULLS LAST, cycle_time DESC, run_id DESC
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
                    if row:
                        identity.update(dict(row))
                    if "source" not in identity or "cycle_time" not in identity:
                        cursor.execute(
                            """
                            SELECT source_id AS source, cycle_time
                            FROM met.forecast_cycle
                            WHERE source_id IS NOT NULL AND cycle_time IS NOT NULL
                            ORDER BY cycle_time DESC, cycle_id DESC
                            LIMIT 1
                            """
                        )
                        row = cursor.fetchone()
                        if row:
                            identity.update({key: value for key, value in dict(row).items() if value is not None})
                    cursor.execute(
                        """
                        SELECT job_id
                        FROM ops.pipeline_job
                        WHERE log_uri IS NOT NULL
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, job_id DESC
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
                    if row:
                        identity["job_id"] = row["job_id"]
        except psycopg2.Error as error:
            identity.setdefault("blockers", []).append(
                {
                    "code": "READONLY_DB_IDENTITY_DISCOVERY_BLOCKED",
                    "reason": _safe_db_error_message(error),
                    "sqlstate": getattr(error, "pgcode", None),
                }
            )
        return _json_ready(identity)

    def schema_exists(self, schema: str) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regnamespace(%s) IS NOT NULL AS exists", (schema,))
                return bool(cursor.fetchone()["exists"])

    def table_exists(self, target: ProbeTarget) -> bool:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (target.qualified_name,))
                return bool(cursor.fetchone()["exists"])

    def table_privileges(self, target: ProbeTarget) -> dict[str, bool]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        has_table_privilege(current_user, %s, 'INSERT') AS insert,
                        has_table_privilege(current_user, %s, 'UPDATE') AS update,
                        has_table_privilege(current_user, %s, 'DELETE') AS delete,
                        has_table_privilege(current_user, %s, 'TRUNCATE') AS truncate,
                        has_table_privilege(current_user, %s, 'REFERENCES') AS references,
                        has_table_privilege(current_user, %s, 'TRIGGER') AS trigger
                    """,
                    (
                        target.qualified_name,
                        target.qualified_name,
                        target.qualified_name,
                        target.qualified_name,
                        target.qualified_name,
                        target.qualified_name,
                    ),
                )
                privileges = {key: bool(value) for key, value in dict(cursor.fetchone()).items()}
                privileges.update(self._optional_maintain_privilege_for_current_user(cursor, target))
                return privileges

    def column_privileges(self, target: ProbeTarget) -> dict[str, list[str]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        a.attname AS column_name,
                        has_column_privilege(current_user, c.oid, a.attname, 'INSERT') AS insert,
                        has_column_privilege(current_user, c.oid, a.attname, 'UPDATE') AS update
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid
                    WHERE n.nspname = %s
                      AND c.relname = %s
                      AND c.relkind IN ('r', 'p')
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                      AND a.attgenerated = ''
                    ORDER BY a.attnum
                    """,
                    (target.schema, target.table),
                )
                rows = cursor.fetchall()
        privileges = {"insert": [], "update": []}
        for row in rows:
            column_name = str(row["column_name"])
            if row.get("insert"):
                privileges["insert"].append(column_name)
            if row.get("update"):
                privileges["update"].append(column_name)
        return privileges

    def sequence_privileges(self, target: ProbeTarget) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        seq_ns.nspname AS sequence_schema,
                        seq.relname AS sequence_name,
                        seq_ns.nspname || '.' || seq.relname AS qualified_name,
                        array_remove(array_agg(DISTINCT a.attname ORDER BY a.attname), NULL) AS columns,
                        has_sequence_privilege(current_user, seq.oid, 'USAGE') AS usage,
                        has_sequence_privilege(current_user, seq.oid, 'UPDATE') AS update
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_depend d ON d.refobjid = c.oid
                    JOIN pg_class seq ON seq.oid = d.objid
                    JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
                    LEFT JOIN pg_attribute a
                        ON a.attrelid = c.oid
                       AND a.attnum = d.refobjsubid
                       AND NOT a.attisdropped
                    WHERE n.nspname = %s
                      AND c.relname = %s
                      AND c.relkind IN ('r', 'p')
                      AND seq.relkind = 'S'
                      AND d.classid = 'pg_class'::regclass
                      AND d.refclassid = 'pg_class'::regclass
                      AND d.deptype IN ('a', 'i')
                    GROUP BY seq_ns.nspname, seq.relname, seq.oid
                    ORDER BY seq_ns.nspname, seq.relname
                    """,
                    (target.schema, target.table),
                )
                rows = cursor.fetchall()
        return [
            {
                "sequence_schema": str(row["sequence_schema"]),
                "sequence_name": str(row["sequence_name"]),
                "qualified_name": str(row["qualified_name"]),
                "columns": [str(column) for column in (row.get("columns") or [])],
                "usage": bool(row["usage"]),
                "update": bool(row["update"]),
                "mutating_privilege_allowed": bool(row["usage"]) or bool(row["update"]),
            }
            for row in rows
        ]

    def schema_privileges(self, schema: str) -> dict[str, bool]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'CREATE') AS create",
                    (schema,),
                )
                return {key: bool(value) for key, value in dict(cursor.fetchone()).items()}

    def database_privileges(self) -> dict[str, Any]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._database_privileges_for_current_user(cursor)

    def audited_schema_sequence_privileges(self, schemas: tuple[str, ...]) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                return self._audited_schema_sequence_privileges_for_current_user(cursor, schemas)

    def reachable_role_privileges(
        self,
        targets: tuple[ProbeTarget, ...],
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                membership_columns = self._pg_auth_members_columns(cursor)
                roles = self._reachable_roles(cursor, membership_columns=membership_columns)
                findings = []
                for role in roles:
                    role_finding = self._reachable_role_finding(cursor, role, targets=targets, schemas=schemas)
                    if role_finding is not None:
                        findings.append(role_finding)
        return findings

    def first_updatable_column(self, target: ProbeTarget) -> str | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s
                      AND table_name = %s
                      AND is_generated = 'NEVER'
                      AND is_identity = 'NO'
                    ORDER BY ordinal_position
                    LIMIT 1
                    """,
                    (target.schema, target.table),
                )
                row = cursor.fetchone()
        return str(row["column_name"]) if row else None

    def _pg_auth_members_columns(self, cursor: Any) -> set[str]:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'pg_catalog'
              AND table_name = 'pg_auth_members'
            """
        )
        return {str(row["column_name"]) for row in cursor.fetchall()}

    def _reachable_roles(self, cursor: Any, *, membership_columns: set[str]) -> list[dict[str, Any]]:
        set_option = "COALESCE(m.set_option, true)" if "set_option" in membership_columns else "true"
        inherit_option = (
            "COALESCE(m.inherit_option, true)" if "inherit_option" in membership_columns else "true"
        )
        cursor.execute(
            f"""
            WITH RECURSIVE reachable(roleid, depth, path, can_set, can_inherit) AS (
                SELECT
                    m.roleid,
                    1 AS depth,
                    ARRAY[m.roleid] AS path,
                    {set_option} AS can_set,
                    {inherit_option} AS can_inherit
                FROM pg_auth_members m
                JOIN pg_roles current_role ON current_role.oid = m.member
                WHERE current_role.rolname = current_user

                UNION ALL

                SELECT
                    m.roleid,
                    reachable.depth + 1 AS depth,
                    reachable.path || m.roleid AS path,
                    reachable.can_set AND {set_option} AS can_set,
                    reachable.can_inherit AND {inherit_option} AS can_inherit
                FROM pg_auth_members m
                JOIN reachable ON reachable.roleid = m.member
                WHERE NOT m.roleid = ANY(reachable.path)
            )
            SELECT DISTINCT ON (reachable.roleid)
                reachable.roleid,
                pg_roles.rolname,
                pg_roles.rolsuper,
                pg_roles.rolcreatedb,
                pg_roles.rolcreaterole,
                pg_roles.rolcanlogin,
                pg_roles.rolreplication,
                pg_roles.rolbypassrls,
                reachable.depth,
                reachable.can_set,
                reachable.can_inherit
            FROM reachable
            JOIN pg_roles ON pg_roles.oid = reachable.roleid
            WHERE reachable.can_set OR reachable.can_inherit
            ORDER BY reachable.roleid, reachable.can_set DESC, reachable.can_inherit DESC, reachable.depth ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def _reachable_role_finding(
        self,
        cursor: Any,
        role: Mapping[str, Any],
        *,
        targets: tuple[ProbeTarget, ...],
        schemas: tuple[str, ...],
    ) -> dict[str, Any] | None:
        role_name = str(role.get("rolname") or "")
        if not role_name:
            return None
        unsafe_attributes = {
            flag: bool(role.get(flag))
            for flag in ROLE_ATTRIBUTE_WRITE_FLAGS
            if bool(role.get(flag))
        }
        mutating_findings: list[dict[str, Any]] = []
        for target in targets:
            if not self._table_exists_for_cursor(cursor, target):
                continue
            table_privileges = self._table_privileges_for_role(cursor, role_name, target)
            column_privileges = self._column_privileges_for_role(cursor, role_name, target)
            sequence_privileges = self._sequence_privileges_for_role(cursor, role_name, target)
            mutating_findings.extend(
                _target_catalog_findings(
                    target,
                    table_privileges=table_privileges,
                    column_privileges=column_privileges,
                    sequence_privileges=sequence_privileges,
                    reason_prefix="reachable_role_has",
                )
            )
        database_finding = _database_create_catalog_finding(
            self._database_privileges_for_role(cursor, role_name),
            reason_prefix="reachable_role_has",
        )
        if database_finding is not None:
            mutating_findings.append(database_finding)
        mutating_findings.extend(
            _schema_sequence_catalog_findings(
                self._audited_schema_sequence_privileges_for_role(cursor, role_name, schemas),
                reason_prefix="reachable_role_has",
            )
        )
        for schema in schemas:
            if not self._schema_exists_for_cursor(cursor, schema):
                continue
            schema_privileges = self._schema_privileges_for_role(cursor, role_name, schema)
            if schema_privileges.get("create"):
                mutating_findings.append(
                    {
                        "target": f"{schema}.*",
                        "operation": "DDL_CREATE_TABLE",
                        "reason": "reachable_role_has_schema_create_privilege",
                    }
                )
        if not unsafe_attributes and not mutating_findings:
            return None
        reachable_via = []
        if role.get("can_set"):
            reachable_via.append("set_role")
        if role.get("can_inherit"):
            reachable_via.append("inherit")
        return {
            "role_name": redact_text(role_name),
            "reachable_via": reachable_via,
            "membership_depth": int(role.get("depth") or 0),
            "unsafe_role_attributes": unsafe_attributes,
            "mutating_privilege_findings": mutating_findings,
            "reason": "reachable_role_has_mutating_capability",
        }

    def _table_exists_for_cursor(self, cursor: Any, target: ProbeTarget) -> bool:
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (target.qualified_name,))
        return bool(cursor.fetchone()["exists"])

    def _schema_exists_for_cursor(self, cursor: Any, schema: str) -> bool:
        cursor.execute("SELECT to_regnamespace(%s) IS NOT NULL AS exists", (schema,))
        return bool(cursor.fetchone()["exists"])

    def _table_privileges_for_role(
        self,
        cursor: Any,
        role_name: str,
        target: ProbeTarget,
    ) -> dict[str, bool]:
        cursor.execute(
            """
            SELECT
                has_table_privilege(%s, %s, 'INSERT') AS insert,
                has_table_privilege(%s, %s, 'UPDATE') AS update,
                has_table_privilege(%s, %s, 'DELETE') AS delete,
                has_table_privilege(%s, %s, 'TRUNCATE') AS truncate,
                has_table_privilege(%s, %s, 'REFERENCES') AS references,
                has_table_privilege(%s, %s, 'TRIGGER') AS trigger
            """,
            (
                role_name,
                target.qualified_name,
                role_name,
                target.qualified_name,
                role_name,
                target.qualified_name,
                role_name,
                target.qualified_name,
                role_name,
                target.qualified_name,
                role_name,
                target.qualified_name,
            ),
        )
        privileges = {key: bool(value) for key, value in dict(cursor.fetchone()).items()}
        privileges.update(self._optional_maintain_privilege_for_role(cursor, role_name, target))
        return privileges

    def _column_privileges_for_role(
        self,
        cursor: Any,
        role_name: str,
        target: ProbeTarget,
    ) -> dict[str, list[str]]:
        cursor.execute(
            """
            SELECT
                a.attname AS column_name,
                has_column_privilege(%s, c.oid, a.attname, 'INSERT') AS insert,
                has_column_privilege(%s, c.oid, a.attname, 'UPDATE') AS update
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = %s
              AND c.relname = %s
              AND c.relkind IN ('r', 'p')
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND a.attgenerated = ''
            ORDER BY a.attnum
            """,
            (role_name, role_name, target.schema, target.table),
        )
        privileges = {"insert": [], "update": []}
        for row in cursor.fetchall():
            column_name = str(row["column_name"])
            if row.get("insert"):
                privileges["insert"].append(column_name)
            if row.get("update"):
                privileges["update"].append(column_name)
        return privileges

    def _sequence_privileges_for_role(
        self,
        cursor: Any,
        role_name: str,
        target: ProbeTarget,
    ) -> list[dict[str, Any]]:
        cursor.execute(
            """
            SELECT
                seq_ns.nspname AS sequence_schema,
                seq.relname AS sequence_name,
                seq_ns.nspname || '.' || seq.relname AS qualified_name,
                array_remove(array_agg(DISTINCT a.attname ORDER BY a.attname), NULL) AS columns,
                has_sequence_privilege(%s, seq.oid, 'USAGE') AS usage,
                has_sequence_privilege(%s, seq.oid, 'UPDATE') AS update
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_depend d ON d.refobjid = c.oid
            JOIN pg_class seq ON seq.oid = d.objid
            JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
            LEFT JOIN pg_attribute a
                ON a.attrelid = c.oid
               AND a.attnum = d.refobjsubid
               AND NOT a.attisdropped
            WHERE n.nspname = %s
              AND c.relname = %s
              AND c.relkind IN ('r', 'p')
              AND seq.relkind = 'S'
              AND d.classid = 'pg_class'::regclass
              AND d.refclassid = 'pg_class'::regclass
              AND d.deptype IN ('a', 'i')
            GROUP BY seq_ns.nspname, seq.relname, seq.oid
            ORDER BY seq_ns.nspname, seq.relname
            """,
            (role_name, role_name, target.schema, target.table),
        )
        return [
            {
                "sequence_schema": str(row["sequence_schema"]),
                "sequence_name": str(row["sequence_name"]),
                "qualified_name": str(row["qualified_name"]),
                "columns": [str(column) for column in (row.get("columns") or [])],
                "usage": bool(row["usage"]),
                "update": bool(row["update"]),
                "mutating_privilege_allowed": bool(row["usage"]) or bool(row["update"]),
            }
            for row in cursor.fetchall()
        ]

    def _schema_privileges_for_role(
        self,
        cursor: Any,
        role_name: str,
        schema: str,
    ) -> dict[str, bool]:
        cursor.execute(
            "SELECT has_schema_privilege(%s, %s, 'CREATE') AS create",
            (role_name, schema),
        )
        return {key: bool(value) for key, value in dict(cursor.fetchone()).items()}

    def _database_privileges_for_current_user(self, cursor: Any) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT
                current_database() AS database_name,
                has_database_privilege(current_user, current_database(), 'CREATE') AS create
            """
        )
        row = dict(cursor.fetchone())
        return {
            "database_name": str(row.get("database_name") or "current_database"),
            "create": bool(row.get("create")),
        }

    def _database_privileges_for_role(self, cursor: Any, role_name: str) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT
                current_database() AS database_name,
                has_database_privilege(%s, current_database(), 'CREATE') AS create
            """,
            (role_name,),
        )
        row = dict(cursor.fetchone())
        return {
            "database_name": str(row.get("database_name") or "current_database"),
            "create": bool(row.get("create")),
        }

    def _audited_schema_sequence_privileges_for_current_user(
        self,
        cursor: Any,
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not schemas:
            return []
        cursor.execute(
            """
            SELECT
                seq_ns.nspname AS sequence_schema,
                seq.relname AS sequence_name,
                seq_ns.nspname || '.' || seq.relname AS qualified_name,
                has_sequence_privilege(current_user, seq.oid, 'USAGE') AS usage,
                has_sequence_privilege(current_user, seq.oid, 'UPDATE') AS update
            FROM pg_class seq
            JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
            WHERE seq.relkind = 'S'
              AND seq_ns.nspname = ANY(%s)
            ORDER BY seq_ns.nspname, seq.relname
            """,
            (list(schemas),),
        )
        return _sequence_privilege_rows(cursor.fetchall())

    def _audited_schema_sequence_privileges_for_role(
        self,
        cursor: Any,
        role_name: str,
        schemas: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not schemas:
            return []
        cursor.execute(
            """
            SELECT
                seq_ns.nspname AS sequence_schema,
                seq.relname AS sequence_name,
                seq_ns.nspname || '.' || seq.relname AS qualified_name,
                has_sequence_privilege(%s, seq.oid, 'USAGE') AS usage,
                has_sequence_privilege(%s, seq.oid, 'UPDATE') AS update
            FROM pg_class seq
            JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
            WHERE seq.relkind = 'S'
              AND seq_ns.nspname = ANY(%s)
            ORDER BY seq_ns.nspname, seq.relname
            """,
            (role_name, role_name, list(schemas)),
        )
        return _sequence_privilege_rows(cursor.fetchall())

    def _optional_maintain_privilege_for_current_user(
        self,
        cursor: Any,
        target: ProbeTarget,
    ) -> dict[str, bool]:
        try:
            cursor.execute(
                "SELECT has_table_privilege(current_user, %s, 'MAINTAIN') AS maintain",
                (target.qualified_name,),
            )
        except psycopg2.Error:
            cursor.connection.rollback()
            return {"maintain": False, "maintain_supported": False}
        return {"maintain": bool(cursor.fetchone()["maintain"]), "maintain_supported": True}

    def _optional_maintain_privilege_for_role(
        self,
        cursor: Any,
        role_name: str,
        target: ProbeTarget,
    ) -> dict[str, bool]:
        try:
            cursor.execute(
                "SELECT has_table_privilege(%s, %s, 'MAINTAIN') AS maintain",
                (role_name, target.qualified_name),
            )
        except psycopg2.Error:
            cursor.connection.rollback()
            return {"maintain": False, "maintain_supported": False}
        return {"maintain": bool(cursor.fetchone()["maintain"]), "maintain_supported": True}

    def execute_probe(self, spec: PermissionProbeSpec) -> ProbeExecution:
        connection = psycopg2.connect(self.database_url, **_validation_connect_kwargs())
        try:
            with connection.cursor() as cursor:
                cursor.execute(self._probe_sql(spec))
                rowcount = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
            connection.rollback()
            return ProbeExecution(
                outcome="succeeded",
                rowcount=rowcount,
                message="Probe statement executed; transaction rolled back for cleanup.",
                rolled_back=True,
            )
        except psycopg2.Error as error:
            connection.rollback()
            outcome = "denied" if _is_permission_denied(error) else "blocked"
            return ProbeExecution(
                outcome=outcome,
                sqlstate=getattr(error, "pgcode", None),
                message=_safe_db_error_message(error),
                rolled_back=True,
            )
        finally:
            connection.close()

    def _probe_sql(self, spec: PermissionProbeSpec) -> sql.SQL:
        if spec.operation == "INSERT" and spec.target is not None and spec.probe_column is not None:
            return sql.SQL("INSERT INTO {}.{} ({}) SELECT {} FROM {}.{} WHERE FALSE").format(
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
                sql.Identifier(spec.probe_column),
                sql.Identifier(spec.probe_column),
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
            )
        if spec.operation == "UPDATE" and spec.target is not None and spec.probe_column is not None:
            return sql.SQL("UPDATE {}.{} SET {} = {} WHERE FALSE").format(
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
                sql.Identifier(spec.probe_column),
                sql.Identifier(spec.probe_column),
            )
        if spec.operation == "DELETE" and spec.target is not None:
            return sql.SQL("DELETE FROM {}.{} WHERE FALSE").format(
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
            )
        if spec.operation == "DDL_CREATE_TABLE" and spec.ddl_schema is not None and spec.ddl_table is not None:
            return sql.SQL("CREATE TABLE {}.{} (id integer)").format(
                sql.Identifier(spec.ddl_schema),
                sql.Identifier(spec.ddl_table),
            )
        raise ReadonlyDbValidationError("READONLY_DB_PROBE_INVALID", f"Unsupported probe operation {spec.operation}.")

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = psycopg2.connect(
            self.database_url,
            cursor_factory=RealDictCursor,
            **_validation_connect_kwargs(),
        )
        try:
            yield connection
            connection.rollback()
        finally:
            connection.close()


def validate_readonly_db_boundary(
    config: ReadonlyDbValidationConfig,
    *,
    adapter: ReadonlyDbProbeAdapter | None = None,
    route_requester: RouteRequester | None = None,
    manual_action_probe_runner: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    config = replace(config, evidence_root=_safe_resolved_evidence_root(config.evidence_root))
    provenance = _validation_provenance(
        adapter_injected=adapter is not None,
        route_requester_injected=route_requester is not None,
        manual_action_probe_runner_injected=manual_action_probe_runner is not None,
    )
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()
    if config.force:
        for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
            writer.remove_json(config.lane_dir / filename)
    writer.write_json(
        config.lane_dir / "summary.json",
        _blocked_summary(
            config,
            code="READONLY_DB_VALIDATION_IN_PROGRESS",
            message="Readonly DB validation started; final summary has not been written yet.",
            provenance=provenance,
        ),
    )

    try:
        return _validate_readonly_db_boundary_prepared(
            config,
            writer=writer,
            provenance=provenance,
            adapter=adapter,
            route_requester=route_requester,
            manual_action_probe_runner=manual_action_probe_runner,
        )
    except Exception as error:
        if (
            isinstance(error, ReadonlyDbValidationError)
            and error.error_code.startswith("READONLY_DB_EVIDENCE_")
        ):
            raise
        summary = _unexpected_validation_error_summary(config, error=error, provenance=provenance)
        writer.write_json(config.lane_dir / "summary.json", summary)
        return redact_payload(summary)


def merge_readonly_db_source_evidence(
    *,
    evidence_root: Path,
    run_id: str,
    source_dirs: Sequence[Path],
    force: bool = False,
) -> dict[str, Any]:
    config = ReadonlyDbValidationConfig.from_env(
        evidence_root=evidence_root,
        run_id=run_id,
        database_url="postgresql://redacted/nhms",
        force=force,
    )
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()
    if config.force:
        for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
            writer.remove_json(config.lane_dir / filename)
    source_evidence = [_read_source_evidence(source_dir, expected_run_id=config.run_id) for source_dir in source_dirs]
    summary = _merged_readonly_db_summary(config, source_evidence=source_evidence)
    writer.write_json(config.lane_dir / "role.json", summary["role"])
    writer.write_json(config.lane_dir / "route_smoke.json", summary["route_smoke"])
    writer.write_json(config.lane_dir / "permission_probes.json", summary["permission_probes"])
    writer.write_json(config.lane_dir / "summary.json", summary)
    return redact_payload(summary)


def _read_source_evidence(source_dir: Path, *, expected_run_id: str) -> ReadonlyDbMergeSourceEvidence:
    resolved_dir = _safe_merge_source_dir(source_dir)
    artifacts: dict[str, dict[str, Any]] = {}
    payloads: dict[str, Any] = {}
    for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
        path = resolved_dir / filename
        payloads[filename] = _read_source_json_file(path, source_dir=resolved_dir)
        artifacts[filename] = _source_artifact_record(path, payloads[filename])
    summary = payloads["summary.json"]
    if not isinstance(summary, dict):
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_JSON_INVALID",
            f"Readonly DB source summary must be a JSON object: {resolved_dir / 'summary.json'}",
        )
    _validate_source_siblings(resolved_dir, summary, payloads)
    _validate_merge_source_run(summary, artifacts=artifacts, expected_run_id=expected_run_id)
    return ReadonlyDbMergeSourceEvidence(source_dir=resolved_dir, summary=summary, artifacts=artifacts)


def _safe_merge_source_dir(source_dir: Path) -> Path:
    _refuse_symlink_components(source_dir)
    resolved = _safe_resolved_evidence_root(source_dir)
    _refuse_symlink_components(resolved)
    if resolved.exists() and not resolved.is_dir():
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_INVALID",
            f"Readonly DB merge source must be a directory: {resolved}",
        )
    return resolved


def _read_source_json_file(path: Path, *, source_dir: Path) -> Any:
    try:
        if path.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_SYMLINK",
                f"Readonly DB merge source file must not be a symlink: {path}",
            )
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.resolve(strict=False).parent != source_dir:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_PATH_UNSAFE",
                f"Readonly DB merge source file must stay in source dir: {path}",
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_MISSING",
            f"Readonly DB source authoritative evidence is missing: {path}",
        ) from error
    except json.JSONDecodeError as error:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_JSON_INVALID",
            f"Readonly DB source evidence is invalid JSON: {path}",
        ) from error
    return payload


def _source_artifact_record(path: Path, payload: Any) -> dict[str, Any]:
    return {
        "path": _public_path(path),
        "sha256": _file_sha256(path),
        "run_id": _artifact_run_id(payload),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_run_id(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        value = payload.get("run_id") or payload.get("evidence_run_id") or payload.get("bundle_run_id")
        if value is not None and str(value).strip():
            return str(value)
    if isinstance(payload, list):
        for item in payload:
            value = _artifact_run_id(item)
            if value:
                return value
    return None


def _validate_source_siblings(source_dir: Path, summary: Mapping[str, Any], payloads: Mapping[str, Any]) -> None:
    expected = {
        "role.json": summary.get("role"),
        "route_smoke.json": summary.get("route_smoke"),
        "permission_probes.json": summary.get("permission_probes"),
    }
    for filename, expected_payload in expected.items():
        if payloads.get(filename) != expected_payload:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_SIBLING_MISMATCH",
                f"Readonly DB merge source {filename} must match summary.json: {source_dir}",
            )


def _validate_merge_source_run(
    summary: Mapping[str, Any],
    *,
    artifacts: Mapping[str, Mapping[str, Any]],
    expected_run_id: str,
) -> None:
    run_id = summary.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_RUN_ID_MISSING",
            "Readonly DB merge source summary must include run_id.",
        )
    if run_id == expected_run_id:
        raise ReadonlyDbValidationError(
            "READONLY_DB_MERGE_SOURCE_RUN_LAYOUT_INVALID",
            "Readonly DB merge source must be a per-source lane, not the final merge lane.",
        )
    for filename, artifact in artifacts.items():
        artifact_run_id = artifact.get("run_id")
        if artifact_run_id is not None and artifact_run_id != run_id:
            raise ReadonlyDbValidationError(
                "READONLY_DB_MERGE_SOURCE_RUN_ID_MISMATCH",
                f"Readonly DB merge source artifact {filename} run_id must match summary.json.",
            )


def _merged_readonly_db_summary(
    config: ReadonlyDbValidationConfig,
    *,
    source_evidence: Sequence[ReadonlyDbMergeSourceEvidence],
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    route_smoke: list[dict[str, Any]] = []
    display_identity: dict[str, Any] = {}
    permission_probes: list[dict[str, Any]] | None = None
    role: dict[str, Any] | None = None
    database_url = "postgresql://db.example:5432/nhms"
    source_artifacts: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for index, evidence in enumerate(source_evidence):
        payload = evidence.summary
        if payload.get("schema") != LIVE_EVIDENCE_SCHEMA:
            blockers.append(
                {
                    "code": "READONLY_DB_MERGE_SOURCE_SCHEMA_INVALID",
                    "source_index": index,
                    "schema": payload.get("schema"),
                }
            )
        if payload.get("status") != STATUS_PASS:
            blockers.append(
                {
                    "code": "READONLY_DB_MERGE_SOURCE_NOT_PASS",
                    "source_index": index,
                    "status": payload.get("status"),
                }
            )
        database_url = str(payload.get("database_url") or database_url)
        source_names = _merge_payload_sources(payload)
        if not source_names:
            blockers.append({"code": "READONLY_DB_MERGE_SOURCE_IDENTITY_MISSING", "source_index": index})
        for source_name in source_names:
            if source_name in seen_sources:
                blockers.append(
                    {
                        "code": "READONLY_DB_MERGE_DUPLICATE_SOURCE",
                        "source_index": index,
                        "source": source_name,
                    }
                )
            seen_sources.add(source_name)
        source_artifacts.append(
            {
                "source_index": index,
                "sources": sorted(source_names),
                "source_dir": _public_path(evidence.source_dir),
                "summary_run_id": payload.get("run_id"),
                "artifacts": evidence.artifacts,
            }
        )
        payload_role = payload.get("role")
        if isinstance(payload_role, Mapping):
            role = dict(payload_role) if role is None else role
            if role != payload_role:
                blockers.append({"code": "READONLY_DB_MERGE_ROLE_MISMATCH", "source_index": index})
        payload_permission_probes = _normalized_merge_permission_probes(payload.get("permission_probes"))
        if permission_probes is None and payload_permission_probes is not None:
            permission_probes = payload_permission_probes
        elif payload_permission_probes != permission_probes:
            blockers.append({"code": "READONLY_DB_MERGE_PERMISSION_MATRIX_MISMATCH", "source_index": index})
        for route in payload.get("route_smoke", []):
            if isinstance(route, Mapping):
                if route.get("name") in {"health", "runtime_config", "models"} and any(
                    existing.get("name") == route.get("name") for existing in route_smoke
                ):
                    continue
                route_record = dict(route)
                identity = route_record.get("strict_identity") or route_record.get("identity")
                if "source" not in route_record and isinstance(identity, Mapping) and identity.get("source"):
                    route_record["source"] = str(identity["source"])
                if "source" not in route_record:
                    source_from_path = _source_from_route_path(str(route_record.get("path") or ""))
                    if source_from_path:
                        route_record["source"] = source_from_path
                route_smoke.append(route_record)
        source_identity = payload.get("display_identity")
        if isinstance(source_identity, Mapping):
            source_name = _identity_text(source_identity, "source")
            if source_name:
                display_identity[source_name] = dict(source_identity)
            else:
                for key, value in source_identity.items():
                    if isinstance(value, Mapping):
                        display_identity[str(key)] = dict(value)
    if role is None:
        role = {"current_user": None, "role_type": "readonly_candidate"}
        blockers.append({"code": "READONLY_DB_MERGE_ROLE_MISSING"})
    if permission_probes is None:
        permission_probes = []
        blockers.append({"code": "READONLY_DB_MERGE_PERMISSION_MATRIX_MISSING"})
    missing_sources = sorted({"GFS", "IFS"} - seen_sources)
    if missing_sources:
        blockers.append(
            {
                "code": "READONLY_DB_MERGE_SOURCE_MISSING",
                "missing_sources": missing_sources,
                "observed_sources": sorted(seen_sources),
            }
        )
    status = STATUS_BLOCKED if blockers else STATUS_PASS
    summary = {
        "schema": LIVE_EVIDENCE_SCHEMA,
        "status": status,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(database_url),
        "source_env_vars": list(READONLY_DB_URL_ENVS),
        "validation_provenance": {
            "mode": "live",
            "live_readonly_proof": not blockers,
            "merged_source_evidence": True,
            "source_bundle_count": len(source_evidence),
            "source_artifacts": source_artifacts,
        },
        "validation_timeouts": _validation_timeout_evidence(),
        "runtime": {
            "service_role": "display_readonly",
            "control_mutations_expected": False,
        },
        "role": role,
        "display_identity": display_identity,
        "route_smoke": route_smoke,
        "manual_action_probes": _merged_manual_actions([evidence.summary for evidence in source_evidence]),
        "permission_probe_summary": _permission_summary(permission_probes),
        "permission_probes": permission_probes,
        "redaction": {
            "database_url_redacted": True,
            "sensitive_values_redacted": True,
            "evidence_root_approved": True,
        },
    }
    if blockers:
        summary["blockers"] = blockers
    return summary


def _merged_manual_actions(source_payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    for payload in source_payloads:
        manual_actions = payload.get("manual_action_probes")
        if isinstance(manual_actions, list) and manual_actions:
            return [dict(item) for item in manual_actions if isinstance(item, Mapping)]
    return []


def _merge_payload_sources(payload: Mapping[str, Any]) -> set[str]:
    sources: set[str] = set()
    display_identity = payload.get("display_identity")
    if isinstance(display_identity, Mapping):
        source_name = _identity_text(display_identity, "source")
        if source_name:
            sources.add(source_name.upper())
        for key, value in display_identity.items():
            if str(key).upper() in {"GFS", "IFS"} and isinstance(value, Mapping):
                sources.add(str(key).upper())
            if isinstance(value, Mapping):
                nested_source = _identity_text(value, "source")
                if nested_source:
                    sources.add(nested_source.upper())
    route_smoke = payload.get("route_smoke")
    if isinstance(route_smoke, list):
        for route in route_smoke:
            if not isinstance(route, Mapping):
                continue
            source = _identity_text(route, "source") or _identity_text(route, "source_id")
            identity = route.get("strict_identity") or route.get("identity")
            if not source and isinstance(identity, Mapping):
                source = _identity_text(identity, "source") or _identity_text(identity, "source_id")
            if source:
                sources.add(source.upper())
    return sources


def _source_from_route_path(path: str) -> str | None:
    try:
        query = parse_qsl(urlsplit(path).query, keep_blank_values=True)
    except ValueError:
        return None
    values = {key: value for key, value in query}
    return values.get("source") or values.get("source_id")


def _normalized_merge_permission_probes(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    probes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        probe = dict(item)
        operations = probe.get("operations")
        if isinstance(operations, list):
            normalized_operations = []
            for operation in operations:
                if not isinstance(operation, Mapping):
                    continue
                normalized = dict(operation)
                normalized.pop("command", None)
                normalized_operations.append(normalized)
            probe["operations"] = normalized_operations
        probes.append(probe)
    return probes


def _validate_readonly_db_boundary_prepared(
    config: ReadonlyDbValidationConfig,
    *,
    writer: EvidenceWriter,
    provenance: Mapping[str, Any],
    adapter: ReadonlyDbProbeAdapter | None,
    route_requester: RouteRequester | None,
    manual_action_probe_runner: Callable[[str], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    if not config.database_url:
        summary = _blocked_summary(
            config,
            code="READONLY_DB_URL_MISSING",
            message=(
                "A real readonly database URL is required via NHMS_DISPLAY_READONLY_DATABASE_URL, "
                "NHMS_READONLY_DB_VALIDATION_DATABASE_URL, or --database-url."
            ),
            provenance=provenance,
        )
        writer.write_json(config.lane_dir / "summary.json", summary)
        return summary

    database_url = config.database_url
    adapter = adapter or PsycopgReadonlyDbProbeAdapter(database_url, ddl_suffix=_ddl_suffix(config.run_id))

    try:
        role = adapter.current_role()
    except psycopg2.OperationalError as error:
        summary = _blocked_summary(
            config,
            code="READONLY_DB_CONNECT_FAILED",
            message=_safe_db_error_message(error),
            provenance=provenance,
        )
        writer.write_json(config.lane_dir / "summary.json", summary)
        return summary

    discovered_identity = _safe_discover_identity(adapter)
    identity = _merged_identity(config, discovered_identity)
    permission_probes = run_permission_probe_matrix(adapter, ddl_suffix=_ddl_suffix(config.run_id))
    role_evidence = _role_evidence(role, permission_probes)
    route_smoke = run_display_route_smoke(config, identity, route_requester=route_requester)
    manual_actions = (
        manual_action_probe_runner(_manual_action_run_id(identity))
        if manual_action_probe_runner is not None
        else run_display_manual_action_probes(_manual_action_run_id(identity), database_url=database_url)
    )
    status = _overall_status(
        role_evidence=role_evidence,
        permission_probes=permission_probes,
        route_smoke=route_smoke,
        manual_actions=manual_actions,
    )
    blockers: list[dict[str, Any]] = []
    if provenance["mode"] == "simulated" and status == STATUS_PASS:
        status = STATUS_BLOCKED
        blockers.append(
            {
                "code": "READONLY_DB_VALIDATION_SIMULATED",
                "message": (
                    "Injected adapter/requester/manual probe results are test-only and cannot be used as "
                    "live readonly DB PASS evidence."
                ),
                "injected_components": provenance["injected_components"],
            }
        )
    summary = {
        "schema": _evidence_schema(provenance),
        "status": status,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(database_url),
        "source_env_vars": list(READONLY_DB_URL_ENVS),
        "validation_provenance": provenance,
        "validation_timeouts": _validation_timeout_evidence(),
        "runtime": {
            "service_role": "display_readonly",
            "control_mutations_expected": False,
        },
        "role": role_evidence,
        "display_identity": identity,
        "route_smoke": route_smoke,
        "manual_action_probes": manual_actions,
        "permission_probe_summary": _permission_summary(permission_probes),
        "permission_probes": permission_probes,
        "redaction": {
            "database_url_redacted": True,
            "sensitive_values_redacted": True,
            "evidence_root_approved": True,
        },
    }
    if blockers:
        summary["blockers"] = blockers
    writer.write_json(config.lane_dir / "role.json", role_evidence)
    writer.write_json(config.lane_dir / "route_smoke.json", route_smoke)
    writer.write_json(config.lane_dir / "permission_probes.json", permission_probes)
    writer.write_json(config.lane_dir / "summary.json", summary)
    return redact_payload(summary)


def run_permission_probe_matrix(
    adapter: ReadonlyDbProbeAdapter,
    *,
    ddl_suffix: str,
) -> list[dict[str, Any]]:
    catalog = _collect_permission_catalog(adapter)
    catalog_has_mutating_privilege = _catalog_has_mutating_privilege(catalog)
    sequence_has_mutating_privilege = _catalog_has_sequence_mutating_privilege(catalog)
    results = [
        _database_probe_result(catalog["database_privileges"]),
        _audited_schema_sequence_probe_result(catalog["audited_schema_sequence_privileges"]),
        _role_membership_probe_result(catalog["reachable_role_privileges"]),
        *[
            _table_probe_result(
                adapter,
                target,
                catalog["targets"][target.qualified_name],
                skip_due_to_catalog_mutating_privilege=catalog_has_mutating_privilege,
            )
            for target in PERMISSION_PROBE_TARGETS
        ],
    ]
    results.extend(
        _schema_probe_result(
            adapter,
            schema,
            catalog["schemas"][schema],
            ddl_suffix=ddl_suffix,
            skip_due_to_catalog_mutating_privilege=catalog_has_mutating_privilege,
            skip_due_to_sequence_mutating_privilege=sequence_has_mutating_privilege,
        )
        for schema in catalog["schema_order"]
    )
    return results


def run_display_route_smoke(
    config: ReadonlyDbValidationConfig,
    identity: Mapping[str, Any],
    *,
    route_requester: RouteRequester | None = None,
) -> list[dict[str, Any]]:
    routes = _display_read_routes(identity)
    if route_requester is not None:
        return [_route_result(spec, route_requester=route_requester) for spec in routes]
    with _fastapi_display_route_requester(config.database_url or "") as requester:
        return [_route_result(spec, route_requester=requester) for spec in routes]


def run_display_manual_action_probes(run_id: str, *, database_url: str | None = None) -> list[dict[str, Any]]:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app
    from apps.api.routes import pipeline as pipeline_routes

    def forbidden_dependency() -> None:
        raise AssertionError("display_readonly manual action probe reached a write or gateway dependency")

    results: list[dict[str, Any]] = []
    validation_env = _display_validation_env()
    if database_url:
        validation_env["DATABASE_URL"] = _bounded_database_url(database_url)
    with _temporary_env(validation_env):
        app = create_app(_display_app_env())
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = forbidden_dependency
        app.dependency_overrides[pipeline_routes.get_retry_service] = forbidden_dependency
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = forbidden_dependency
        with TestClient(app) as client:
            for action in ("retry", "cancel"):
                path = f"/api/v1/runs/{run_id}/{action}"
                try:
                    response = client.post(path, headers=_operator_headers())
                    body = _response_body(response)
                    error = body.get("error") if isinstance(body, dict) else {}
                    passed = response.status_code == 409 and error.get("code") == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
                    results.append(
                        {
                            "name": f"display_{action}_manual_action",
                            "method": "POST",
                            "path": path,
                            "status": STATUS_PASS if passed else STATUS_FAIL,
                            "http_status": response.status_code,
                            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                            "observed_error_code": error.get("code"),
                            "write_dependency_constructed": False,
                            "database_url_configured": bool(database_url),
                        }
                    )
                except AssertionError as error:
                    results.append(
                        {
                            "name": f"display_{action}_manual_action",
                            "method": "POST",
                            "path": path,
                            "status": STATUS_FAIL,
                            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                            "write_dependency_constructed": True,
                            "database_url_configured": bool(database_url),
                            "reason": redact_text(str(error)),
                        }
                    )
    return results


def _collect_permission_catalog(adapter: ReadonlyDbProbeAdapter) -> dict[str, Any]:
    schemas = _permission_probe_schemas()
    target_catalog: dict[str, dict[str, Any]] = {}
    for target in PERMISSION_PROBE_TARGETS:
        exists = adapter.table_exists(target)
        if not exists:
            target_catalog[target.qualified_name] = {"target": target, "exists": False}
            continue
        target_catalog[target.qualified_name] = {
            "target": target,
            "exists": True,
            "table_privileges": adapter.table_privileges(target),
            "column_privileges": adapter.column_privileges(target),
            "sequence_privileges": adapter.sequence_privileges(target),
            "probe_column": adapter.first_updatable_column(target),
        }
    schema_catalog: dict[str, dict[str, Any]] = {}
    for schema in schemas:
        exists = adapter.schema_exists(schema)
        schema_catalog[schema] = {
            "schema": schema,
            "exists": exists,
            "schema_privileges": adapter.schema_privileges(schema) if exists else {},
        }
    return {
        "database_privileges": adapter.database_privileges(),
        "audited_schema_sequence_privileges": adapter.audited_schema_sequence_privileges(schemas),
        "targets": target_catalog,
        "schemas": schema_catalog,
        "schema_order": schemas,
        "reachable_role_privileges": adapter.reachable_role_privileges(PERMISSION_PROBE_TARGETS, schemas),
    }


def _permission_probe_schemas() -> tuple[str, ...]:
    return tuple(sorted({target.schema for target in PERMISSION_PROBE_TARGETS}))


def _catalog_has_mutating_privilege(catalog: Mapping[str, Any]) -> bool:
    if catalog.get("reachable_role_privileges"):
        return True
    if catalog.get("database_privileges", {}).get("create"):
        return True
    if _sequence_mutating_privilege(list(catalog.get("audited_schema_sequence_privileges", [])))["allowed"]:
        return True
    for target_catalog in catalog.get("targets", {}).values():
        if not target_catalog.get("exists"):
            continue
        if _target_has_catalog_mutating_privilege(target_catalog):
            return True
    for schema_catalog in catalog.get("schemas", {}).values():
        privileges = schema_catalog.get("schema_privileges", {})
        if schema_catalog.get("exists") and privileges.get("create"):
            return True
    return False


def _catalog_has_sequence_mutating_privilege(catalog: Mapping[str, Any]) -> bool:
    return _sequence_mutating_privilege(list(catalog.get("audited_schema_sequence_privileges", [])))["allowed"] or any(
        _sequence_mutating_privilege(target_catalog.get("sequence_privileges", []))["allowed"]
        for target_catalog in catalog.get("targets", {}).values()
        if target_catalog.get("exists")
    )


def _target_has_catalog_mutating_privilege(target_catalog: Mapping[str, Any]) -> bool:
    target = target_catalog["target"]
    return bool(
        _target_catalog_findings(
            target,
            table_privileges=target_catalog.get("table_privileges", {}),
            column_privileges=target_catalog.get("column_privileges", {}),
            sequence_privileges=target_catalog.get("sequence_privileges", []),
        )
    )


def _target_catalog_findings(
    target: ProbeTarget,
    *,
    table_privileges: Mapping[str, bool],
    column_privileges: Mapping[str, list[str]],
    sequence_privileges: list[dict[str, Any]],
    reason_prefix: str = "tested_credential_has",
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for operation in TABLE_CATALOG_MUTATING_OPERATIONS:
        privilege = _catalog_mutating_privilege(
            table_privileges,
            column_privileges,
            operation=operation,
            reason_prefix=reason_prefix,
        )
        if not privilege["allowed"]:
            continue
        finding = {
            "target": target.qualified_name,
            "operation": operation,
            "reason": privilege["reason"],
        }
        if privilege.get("columns"):
            finding["columns"] = privilege["columns"]
        findings.append(finding)
    sequence_privilege = _sequence_mutating_privilege(sequence_privileges, reason_prefix=reason_prefix)
    if sequence_privilege["allowed"]:
        findings.append(
            {
                "target": target.qualified_name,
                "operation": "SEQUENCE_USAGE_UPDATE",
                "reason": sequence_privilege["reason"],
                "sequences": sequence_privilege["sequences"],
            }
        )
    return findings


def _database_create_catalog_finding(
    database_privileges: Mapping[str, Any],
    *,
    reason_prefix: str = "tested_credential_has",
) -> dict[str, Any] | None:
    if not database_privileges.get("create"):
        return None
    database_name = str(database_privileges.get("database_name") or "current_database")
    return {
        "target": database_name,
        "operation": "DATABASE_CREATE",
        "reason": f"{reason_prefix}_database_create_privilege",
        "database_name": database_name,
    }


def _schema_sequence_catalog_findings(
    sequence_privileges: list[dict[str, Any]],
    *,
    reason_prefix: str = "tested_credential_has",
) -> list[dict[str, Any]]:
    sequence_privilege = _sequence_mutating_privilege(sequence_privileges, reason_prefix=reason_prefix)
    if not sequence_privilege["allowed"]:
        return []
    return [
        {
            "target": "audited_schema_sequences",
            "operation": "AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE",
            "reason": sequence_privilege["reason"],
            "sequences": sequence_privilege["sequences"],
        }
    ]


def _database_probe_result(database_privileges: Mapping[str, Any]) -> dict[str, Any]:
    database_name = str(database_privileges.get("database_name") or "current_database")
    privilege = _database_create_mutating_privilege(database_privileges)
    spec = PermissionProbeSpec(
        operation="DATABASE_CREATE",
        target=None,
        command=f"CATALOG CHECK CREATE privilege on current database {database_name}",
    )
    if privilege["allowed"]:
        operation = _catalog_short_circuit_operation_result(spec, privilege=privilege)
    else:
        operation = {
            "operation": spec.operation,
            "command": spec.command,
            "status": STATUS_PASS,
            "privilege_allowed": False,
            "table_privilege_allowed": False,
            "column_privilege_allowed": False,
            "sequence_privilege_allowed": False,
            "schema_privilege_allowed": False,
            "database_privilege_allowed": False,
            "database_name": database_name,
            "execution_outcome": "catalog_checked_no_database_create_privilege",
            "rolled_back": False,
            "reason": "tested_credential_lacks_database_create_privilege",
        }
    return {
        "target": database_name,
        "surface": "current_database_create_catalog",
        "status": operation["status"],
        "database_privileges": dict(database_privileges),
        "operations": [operation],
    }


def _audited_schema_sequence_probe_result(sequence_privileges: list[dict[str, Any]]) -> dict[str, Any]:
    privilege = _sequence_mutating_privilege(sequence_privileges)
    spec = PermissionProbeSpec(
        operation="AUDITED_SCHEMA_SEQUENCE_USAGE_UPDATE",
        target=None,
        command="CATALOG CHECK sequence USAGE/UPDATE privileges in audited schemas",
    )
    if privilege["allowed"]:
        operation = _catalog_short_circuit_operation_result(spec, privilege=privilege)
    else:
        operation = {
            "operation": spec.operation,
            "command": spec.command,
            "status": STATUS_PASS,
            "privilege_allowed": False,
            "table_privilege_allowed": False,
            "column_privilege_allowed": False,
            "sequence_privilege_allowed": False,
            "schema_privilege_allowed": False,
            "database_privilege_allowed": False,
            "execution_outcome": "catalog_checked_no_audited_schema_sequence_mutating_privilege",
            "rolled_back": False,
            "reason": "tested_credential_lacks_audited_schema_sequence_mutating_privilege",
        }
    return {
        "target": "audited_schema_sequences",
        "surface": "audited_schema_sequence_catalog",
        "status": operation["status"],
        "sequence_privileges": sequence_privileges,
        "operations": [operation],
    }


def _role_membership_probe_result(reachable_role_privileges: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [
        _reachable_role_operation_result(role_finding)
        for role_finding in reachable_role_privileges
    ]
    return {
        "target": "reachable_roles",
        "surface": "reachable_role_membership",
        "status": _status_from_children(operations) if operations else STATUS_PASS,
        "reachable_role_findings": reachable_role_privileges,
        "operations": operations,
    }


def _reachable_role_operation_result(role_finding: Mapping[str, Any]) -> dict[str, Any]:
    role_name = str(role_finding.get("role_name") or "")
    return {
        "operation": "REACHABLE_ROLE_MEMBERSHIP",
        "command": f"CATALOG CHECK reachable role membership for {role_name}",
        "status": STATUS_FAIL,
        "privilege_allowed": True,
        "table_privilege_allowed": False,
        "column_privilege_allowed": False,
        "sequence_privilege_allowed": False,
        "schema_privilege_allowed": False,
        "execution_outcome": "not_executed_role_membership_catalog_only",
        "rolled_back": False,
        "catalog_short_circuited": True,
        "reason": str(role_finding.get("reason") or "reachable_role_has_mutating_capability"),
        "role_name": role_name,
        "reachable_via": list(role_finding.get("reachable_via", [])),
        "unsafe_role_attributes": dict(role_finding.get("unsafe_role_attributes", {})),
        "mutating_privilege_findings": list(role_finding.get("mutating_privilege_findings", [])),
    }


def _table_probe_result(
    adapter: ReadonlyDbProbeAdapter,
    target: ProbeTarget,
    catalog: Mapping[str, Any],
    *,
    skip_due_to_catalog_mutating_privilege: bool,
) -> dict[str, Any]:
    if not catalog.get("exists"):
        return {
            "target": target.qualified_name,
            "surface": target.surface,
            "status": STATUS_BLOCKED,
            "reason": "required_table_absent_in_fixture",
            "operations": [],
        }
    privileges = dict(catalog.get("table_privileges", {}))
    column_privileges = dict(catalog.get("column_privileges", {}))
    sequence_privileges = list(catalog.get("sequence_privileges", []))
    probe_column = catalog.get("probe_column")
    insert_privilege = _catalog_mutating_privilege(
        privileges,
        column_privileges,
        operation="INSERT",
    )
    update_privilege = _catalog_mutating_privilege(
        privileges,
        column_privileges,
        operation="UPDATE",
    )
    delete_privilege = _catalog_mutating_privilege(
        privileges,
        column_privileges,
        operation="DELETE",
    )
    catalog_only_privileges = {
        operation: _catalog_mutating_privilege(privileges, column_privileges, operation=operation)
        for operation in TABLE_CATALOG_ONLY_MUTATING_OPERATIONS
    }
    sequence_privilege = _sequence_mutating_privilege(sequence_privileges)
    target_has_catalog_mutating_privilege = any(
        privilege["allowed"]
        for privilege in (
            insert_privilege,
            update_privilege,
            delete_privilege,
            sequence_privilege,
            *catalog_only_privileges.values(),
        )
    )
    operations = []
    if sequence_privilege["allowed"]:
        operations.append(_sequence_short_circuit_operation_result(target, sequence_privilege))
    for operation, privilege in catalog_only_privileges.items():
        if privilege["allowed"]:
            operations.append(_table_catalog_short_circuit_operation_result(target, operation, privilege))
    operations.append(
        _dml_probe_or_blocked(
            adapter,
            target=target,
            operation="INSERT",
            command=(
                f"INSERT INTO {target.qualified_name} ({probe_column}) "
                f"SELECT {probe_column} FROM {target.qualified_name} WHERE FALSE"
                if probe_column is not None
                else f"INSERT INTO {target.qualified_name} (<column>) SELECT <column> WHERE FALSE"
            ),
            probe_column=probe_column,
            privilege=insert_privilege,
            skip_due_to_target_catalog_privilege=target_has_catalog_mutating_privilege,
            skip_due_to_catalog_mutating_privilege=skip_due_to_catalog_mutating_privilege,
        )
    )
    operations.append(
        _dml_probe_or_blocked(
            adapter,
            target=target,
            operation="UPDATE",
            command=(
                f"UPDATE {target.qualified_name} SET {probe_column} = {probe_column} WHERE FALSE"
                if probe_column is not None
                else f"UPDATE {target.qualified_name} SET <column> = <column> WHERE FALSE"
            ),
            probe_column=probe_column,
            privilege=update_privilege,
            skip_due_to_target_catalog_privilege=target_has_catalog_mutating_privilege,
            skip_due_to_catalog_mutating_privilege=skip_due_to_catalog_mutating_privilege,
        )
    )
    operations.append(
        _dml_probe_or_blocked(
            adapter,
            target=target,
            operation="DELETE",
            command=f"DELETE FROM {target.qualified_name} WHERE FALSE",
            probe_column=None,
            privilege=delete_privilege,
            skip_due_to_target_catalog_privilege=target_has_catalog_mutating_privilege,
            skip_due_to_catalog_mutating_privilege=skip_due_to_catalog_mutating_privilege,
            requires_probe_column=False,
        )
    )
    return {
        "target": target.qualified_name,
        "surface": target.surface,
        "status": _status_from_children(operations),
        "table_privileges": privileges,
        "column_privileges": column_privileges,
        "sequence_privileges": sequence_privileges,
        "operations": operations,
    }


def _dml_probe_or_blocked(
    adapter: ReadonlyDbProbeAdapter,
    *,
    target: ProbeTarget,
    operation: str,
    command: str,
    probe_column: str | None,
    privilege: dict[str, Any],
    skip_due_to_target_catalog_privilege: bool,
    skip_due_to_catalog_mutating_privilege: bool,
    requires_probe_column: bool = True,
) -> dict[str, Any]:
    spec = PermissionProbeSpec(
        operation=operation,
        target=target,
        probe_column=probe_column,
        command=command,
    )
    if privilege["allowed"]:
        return _dml_operation_result(adapter, spec, privilege=privilege)
    if skip_due_to_target_catalog_privilege:
        return _catalog_target_skip_operation_result(spec)
    if skip_due_to_catalog_mutating_privilege:
        return _catalog_matrix_skip_operation_result(spec)
    if requires_probe_column and probe_column is None:
        return {
            "operation": operation,
            "command": command,
            "status": STATUS_BLOCKED,
            "reason": "no_mutation_probe_column_available",
            "privilege_allowed": False,
            "table_privilege_allowed": False,
            "column_privilege_allowed": False,
            "sequence_privilege_allowed": False,
            "schema_privilege_allowed": False,
            "execution_outcome": "not_executed",
            "rolled_back": False,
        }
    return _dml_operation_result(adapter, spec, privilege=privilege)


def _dml_operation_result(
    adapter: ReadonlyDbProbeAdapter,
    spec: PermissionProbeSpec,
    *,
    privilege: dict[str, Any],
) -> dict[str, Any]:
    if privilege["allowed"]:
        return _catalog_short_circuit_operation_result(spec, privilege=privilege)

    execution = adapter.execute_probe(spec)
    status = _operation_status(execution, privilege_allowed=False)
    result = {
        "operation": spec.operation,
        "command": spec.command,
        "status": status,
        "privilege_allowed": False,
        "table_privilege_allowed": False,
        "column_privilege_allowed": False,
        "sequence_privilege_allowed": False,
        "schema_privilege_allowed": False,
        "execution_outcome": execution.outcome,
        "sqlstate": execution.sqlstate,
        "rolled_back": execution.rolled_back,
    }
    if execution.message:
        result["message"] = execution.message
    if execution.rowcount is not None:
        result["rowcount"] = execution.rowcount
    if execution.outcome == "succeeded":
        result["reason"] = "mutating_probe_executed_successfully_before_rollback"
    elif execution.outcome == "denied":
        result["reason"] = "mutating_probe_denied_before_commit"
    else:
        result["reason"] = "mutating_probe_blocked_by_fixture_or_unexpected_database_error"
    return result


def _catalog_mutating_privilege(
    table_privileges: Mapping[str, bool],
    column_privileges: Mapping[str, list[str]],
    *,
    operation: str,
    reason_prefix: str = "tested_credential_has",
) -> dict[str, Any]:
    key = operation.lower()
    table_allowed = bool(table_privileges.get(key, False))
    columns = list(column_privileges.get(key, [])) if key in {"insert", "update"} else []
    column_allowed = bool(columns)
    if table_allowed:
        reason = f"{reason_prefix}_mutating_table_privilege"
    elif column_allowed:
        reason = f"{reason_prefix}_mutating_column_privilege"
    else:
        reason = None
    return {
        "allowed": table_allowed or column_allowed,
        "reason": reason,
        "table_allowed": table_allowed,
        "column_allowed": column_allowed,
        "sequence_allowed": False,
        "schema_allowed": False,
        "table_privilege": key if table_allowed else None,
        "columns": columns,
    }


def _database_create_mutating_privilege(
    database_privileges: Mapping[str, Any],
    *,
    reason_prefix: str = "tested_credential_has",
) -> dict[str, Any]:
    allowed = bool(database_privileges.get("create", False))
    database_name = str(database_privileges.get("database_name") or "current_database")
    return {
        "allowed": allowed,
        "reason": f"{reason_prefix}_database_create_privilege" if allowed else None,
        "table_allowed": False,
        "column_allowed": False,
        "sequence_allowed": False,
        "schema_allowed": False,
        "database_allowed": allowed,
        "database_name": database_name,
        "columns": [],
    }


def _sequence_privilege_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sequence_schema": str(row["sequence_schema"]),
            "sequence_name": str(row["sequence_name"]),
            "qualified_name": str(row["qualified_name"]),
            "columns": [str(column) for column in (row.get("columns") or [])],
            "usage": bool(row["usage"]),
            "update": bool(row["update"]),
            "mutating_privilege_allowed": bool(row["usage"]) or bool(row["update"]),
        }
        for row in rows
    ]


def _sequence_mutating_privilege(
    sequence_privileges: list[dict[str, Any]],
    *,
    reason_prefix: str = "tested_credential_has",
) -> dict[str, Any]:
    mutating_sequences = [
        {
            "sequence_schema": str(sequence.get("sequence_schema") or ""),
            "sequence_name": str(sequence.get("sequence_name") or ""),
            "qualified_name": str(sequence.get("qualified_name") or ""),
            "columns": [str(column) for column in sequence.get("columns", [])],
            "usage": bool(sequence.get("usage", False)),
            "update": bool(sequence.get("update", False)),
        }
        for sequence in sequence_privileges
        if sequence.get("mutating_privilege_allowed")
        or sequence.get("usage")
        or sequence.get("update")
    ]
    return {
        "allowed": bool(mutating_sequences),
        "reason": (
            f"{reason_prefix}_mutating_sequence_privilege" if mutating_sequences else None
        ),
        "table_allowed": False,
        "column_allowed": False,
        "sequence_allowed": bool(mutating_sequences),
        "schema_allowed": False,
        "columns": [],
        "sequences": mutating_sequences,
    }


def _sequence_short_circuit_operation_result(
    target: ProbeTarget,
    privilege: Mapping[str, Any],
) -> dict[str, Any]:
    spec = PermissionProbeSpec(
        operation="SEQUENCE_USAGE_UPDATE",
        target=target,
        command=f"CATALOG CHECK sequence USAGE/UPDATE privileges for {target.qualified_name}",
    )
    return _catalog_short_circuit_operation_result(spec, privilege=privilege)


def _table_catalog_short_circuit_operation_result(
    target: ProbeTarget,
    operation: str,
    privilege: Mapping[str, Any],
) -> dict[str, Any]:
    spec = PermissionProbeSpec(
        operation=operation,
        target=target,
        command=f"CATALOG CHECK table {operation} privilege for {target.qualified_name}",
    )
    return _catalog_short_circuit_operation_result(spec, privilege=privilege)


def _catalog_short_circuit_operation_result(
    spec: PermissionProbeSpec,
    *,
    privilege: Mapping[str, Any],
) -> dict[str, Any]:
    result = {
        "operation": spec.operation,
        "command": spec.command,
        "status": STATUS_FAIL,
        "privilege_allowed": True,
        "table_privilege_allowed": bool(privilege.get("table_allowed", False)),
        "column_privilege_allowed": bool(privilege.get("column_allowed", False)),
        "sequence_privilege_allowed": bool(privilege.get("sequence_allowed", False)),
        "schema_privilege_allowed": bool(privilege.get("schema_allowed", False)),
        "database_privilege_allowed": bool(privilege.get("database_allowed", False)),
        "execution_outcome": "not_executed_due_to_catalog_mutating_privilege",
        "rolled_back": False,
        "catalog_short_circuited": True,
        "reason": str(privilege.get("reason") or "tested_credential_has_mutating_catalog_privilege"),
    }
    database_name = privilege.get("database_name")
    if database_name:
        result["database_name"] = str(database_name)
    table_privilege = privilege.get("table_privilege")
    if table_privilege:
        result["table_privilege"] = str(table_privilege)
    columns = [str(column) for column in privilege.get("columns", [])]
    if columns:
        result["column_privilege_columns"] = columns
    sequences = [dict(sequence) for sequence in privilege.get("sequences", [])]
    if sequences:
        result["sequence_privilege_sequences"] = sequences
    return result


def _catalog_target_skip_operation_result(spec: PermissionProbeSpec) -> dict[str, Any]:
    return {
        "operation": spec.operation,
        "command": spec.command,
        "status": STATUS_FAIL,
        "privilege_allowed": False,
        "table_privilege_allowed": False,
        "column_privilege_allowed": False,
        "sequence_privilege_allowed": False,
        "schema_privilege_allowed": False,
        "execution_outcome": "not_executed_due_to_target_catalog_mutating_privilege",
        "rolled_back": False,
        "catalog_short_circuited": True,
        "reason": "target_has_catalog_mutating_privilege_probe_skipped",
    }


def _catalog_matrix_skip_operation_result(spec: PermissionProbeSpec) -> dict[str, Any]:
    return {
        "operation": spec.operation,
        "command": spec.command,
        "status": STATUS_FAIL,
        "privilege_allowed": False,
        "table_privilege_allowed": False,
        "column_privilege_allowed": False,
        "sequence_privilege_allowed": False,
        "schema_privilege_allowed": False,
        "execution_outcome": "not_executed_due_to_catalog_mutating_privilege",
        "rolled_back": False,
        "catalog_short_circuited": True,
        "reason": "catalog_mutating_privilege_detected_probe_skipped",
    }


def _schema_probe_result(
    adapter: ReadonlyDbProbeAdapter,
    schema_name: str,
    catalog: Mapping[str, Any],
    *,
    ddl_suffix: str,
    skip_due_to_catalog_mutating_privilege: bool,
    skip_due_to_sequence_mutating_privilege: bool = False,
) -> dict[str, Any]:
    probe_table = f"__nhms_readonly_validation_probe_{ddl_suffix}"
    command = f"CREATE TABLE {schema_name}.{probe_table} (id integer)"
    if not catalog.get("exists"):
        return {
            "target": f"{schema_name}.*",
            "surface": "schema_table_ddl",
            "status": STATUS_BLOCKED,
            "reason": "required_schema_absent_in_fixture",
            "operations": [],
        }
    privileges = dict(catalog.get("schema_privileges", {}))
    privilege_allowed = privileges.get("create", False)
    spec = PermissionProbeSpec(
        operation="DDL_CREATE_TABLE",
        target=None,
        ddl_schema=schema_name,
        ddl_table=probe_table,
        command=command,
    )
    if privilege_allowed:
        operation = _catalog_short_circuit_operation_result(
            spec,
            privilege={
                "allowed": True,
                "reason": "tested_credential_has_schema_create_privilege",
                "table_allowed": False,
                "column_allowed": False,
                "sequence_allowed": False,
                "schema_allowed": True,
                "columns": [],
            },
        )
    elif skip_due_to_sequence_mutating_privilege:
        operation = {
            "operation": "DDL_CREATE_TABLE",
            "command": command,
            "status": STATUS_FAIL,
            "privilege_allowed": False,
            "table_privilege_allowed": False,
            "column_privilege_allowed": False,
            "sequence_privilege_allowed": True,
            "schema_privilege_allowed": False,
            "execution_outcome": "not_executed_due_to_sequence_mutating_privilege",
            "rolled_back": False,
            "catalog_short_circuited": True,
            "reason": "sequence_mutating_privilege_detected_ddl_probe_skipped",
        }
    elif skip_due_to_catalog_mutating_privilege:
        operation = _catalog_matrix_skip_operation_result(spec)
    else:
        execution = adapter.execute_probe(spec)
        operation = {
            "operation": "DDL_CREATE_TABLE",
            "command": command,
            "status": _operation_status(execution, privilege_allowed=False),
            "privilege_allowed": False,
            "table_privilege_allowed": False,
            "column_privilege_allowed": False,
            "sequence_privilege_allowed": False,
            "schema_privilege_allowed": False,
            "execution_outcome": execution.outcome,
            "sqlstate": execution.sqlstate,
            "rolled_back": execution.rolled_back,
            "reason": (
                "ddl_probe_executed_successfully_before_rollback"
                if execution.outcome == "succeeded"
                else "ddl_probe_denied_before_commit"
                if execution.outcome == "denied"
                else "ddl_probe_blocked_by_fixture_or_unexpected_database_error"
            ),
        }
        if execution.message:
            operation["message"] = execution.message
    return {
        "target": f"{schema_name}.*",
        "surface": "schema_table_ddl",
        "status": operation["status"],
        "schema_privileges": privileges,
        "operations": [operation],
    }


def _operation_status(execution: ProbeExecution, *, privilege_allowed: bool) -> str:
    if privilege_allowed or execution.outcome == "succeeded":
        return STATUS_FAIL
    if execution.outcome == "denied":
        return STATUS_PASS
    return STATUS_BLOCKED


def _display_read_routes(identity: Mapping[str, Any]) -> list[dict[str, Any]]:
    model_id = _identity_text(identity, "model_id") or "basins_qhh_shud"
    job_id = _identity_text(identity, "job_id")
    strict_identity, missing_strict_identity = _strict_route_identity(identity)
    latest_route = (
        {
            "name": "latest_product",
            "method": "GET",
            "path": _query_path("/api/v1/mvp/qhh/latest-product", strict_identity),
            "fixture_blocker_allowed": True,
            "strict_identity": strict_identity,
        }
        if strict_identity is not None
        else _strict_identity_blocked_route(
            "latest_product",
            "source_cycle_run_model_required_for_latest_product_smoke",
            missing_strict_identity,
        )
    )
    pipeline_status_route = (
        {
            "name": "pipeline_status",
            "method": "GET",
            "path": _query_path("/api/v1/pipeline/status", strict_identity),
            "fixture_blocker_allowed": True,
            "strict_identity": strict_identity,
        }
        if strict_identity is not None
        else _strict_identity_blocked_route(
            "pipeline_status",
            "source_cycle_run_model_required_for_pipeline_status_smoke",
            missing_strict_identity,
        )
    )
    pipeline_stages_route = (
        {
            "name": "pipeline_stages",
            "method": "GET",
            "path": _query_path("/api/v1/pipeline/stages", strict_identity),
            "fixture_blocker_allowed": True,
            "strict_identity": strict_identity,
        }
        if strict_identity is not None
        else _strict_identity_blocked_route(
            "pipeline_stages",
            "source_cycle_run_model_required_for_pipeline_stages_smoke",
            missing_strict_identity,
        )
    )
    jobs_route = (
        {
            "name": "jobs",
            "method": "GET",
            "path": _query_path("/api/v1/jobs", {**strict_identity, "limit": "1"}),
            "fixture_blocker_allowed": True,
            "strict_identity": strict_identity,
        }
        if strict_identity is not None
        else _strict_identity_blocked_route(
            "jobs",
            "source_cycle_run_model_required_for_jobs_smoke",
            missing_strict_identity,
        )
    )
    job_log_identity, missing_job_log_identity = _strict_route_identity(identity, require_job_id=True)
    job_logs_route = (
        {
            "name": "job_logs",
            "method": "GET",
            "path": _query_path(f"/api/v1/jobs/{_url_value(job_id or '')}/logs", job_log_identity),
            "fixture_blocker_allowed": True,
            "strict_identity": job_log_identity,
        }
        if job_log_identity is not None and job_id is not None
        else _strict_identity_blocked_route(
            "job_logs",
            "source_cycle_run_model_job_required_for_job_log_smoke",
            missing_job_log_identity,
            required_fields=["source", "cycle_time", "run_id", "model_id", "job_id"],
        )
    )
    routes = [
        {"name": "health", "method": "GET", "path": "/health"},
        {"name": "runtime_config", "method": "GET", "path": "/api/v1/runtime/config"},
        {"name": "models", "method": "GET", "path": "/api/v1/models?active=all&limit=1"},
        {
            "name": "stations",
            "method": "GET",
            "path": f"/api/v1/met/stations?model_id={_url_value(model_id)}&limit=1",
        },
        latest_route,
        jobs_route,
        pipeline_status_route,
        pipeline_stages_route,
        job_logs_route,
    ]
    return routes


def _strict_route_identity(
    identity: Mapping[str, Any],
    *,
    require_job_id: bool = False,
) -> tuple[dict[str, str] | None, list[str]]:
    required_fields = ["source", "cycle_time", "run_id", "model_id"]
    if require_job_id:
        required_fields.append("job_id")
    values = {field: _identity_text(identity, field) for field in required_fields}
    missing = [field for field, value in values.items() if not value]
    if missing:
        return None, missing
    return {field: str(value) for field, value in values.items() if field != "job_id"}, []


def _strict_identity_blocked_route(
    name: str,
    reason: str,
    missing_fields: list[str],
    *,
    required_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "method": "GET",
        "path": None,
        "status": STATUS_BLOCKED,
        "reason": reason,
        "strict_identity_required": True,
        "required_identity_fields": required_fields or ["source", "cycle_time", "run_id", "model_id"],
        "missing_identity_fields": missing_fields,
    }


def _query_path(path: str, params: Mapping[str, str]) -> str:
    return f"{path}?{urlencode(params)}"


def _route_result(spec: Mapping[str, Any], *, route_requester: RouteRequester) -> dict[str, Any]:
    if spec.get("status") == STATUS_BLOCKED:
        return dict(spec)
    method = str(spec["method"])
    path = str(spec["path"])
    try:
        response = route_requester(method, path)
    except Exception as error:
        return {
            "name": spec["name"],
            "method": method,
            "path": path,
            "status": STATUS_FAIL,
            "reason": redact_text(str(error)),
        }
    body = response.body if isinstance(response.body, dict) else {}
    error = body.get("error") if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    if 200 <= response.status_code < 300:
        status = STATUS_PASS
        reason = "display_read_route_succeeded"
    elif spec.get("fixture_blocker_allowed") is True and _route_fixture_blocked(response.status_code, error):
        status = STATUS_BLOCKED
        reason = "display_route_fixture_or_published_artifact_blocked"
    else:
        status = STATUS_FAIL
        reason = "display_read_route_failed"
    result = {
        "name": spec["name"],
        "method": method,
        "path": path,
        "status": status,
        "http_status": response.status_code,
        "reason": reason,
    }
    if error:
        result["error_code"] = error.get("code")
        result["error_message"] = error.get("message")
    return result


def _route_fixture_blocked(status_code: int, error: Mapping[str, Any]) -> bool:
    del status_code
    code = str(error.get("code") or "")
    return code in ROUTE_FIXTURE_BLOCKER_ERROR_CODES


def _url_value(value: str) -> str:
    return quote(value, safe="")


@contextmanager
def _fastapi_display_route_requester(database_url: str) -> Iterator[RouteRequester]:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app

    with _temporary_env(_display_validation_env(database_url=database_url)):
        app = create_app(_display_app_env())
        with TestClient(app) as client:

            def requester(method: str, path: str) -> RouteHttpResponse:
                response = client.request(method, path, headers=_operator_headers())
                return RouteHttpResponse(
                    status_code=response.status_code,
                    body=_response_body(response),
                    text=response.text,
                )

            yield requester


def _response_body(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


def _role_evidence(role: Mapping[str, Any], permission_probes: list[dict[str, Any]]) -> dict[str, Any]:
    unsafe_attributes = {flag: bool(role.get(flag)) for flag in ROLE_ATTRIBUTE_WRITE_FLAGS if bool(role.get(flag))}
    privilege_findings = _privilege_findings(permission_probes)
    reachable_role_findings = _reachable_role_findings(permission_probes)
    writer_like = bool(unsafe_attributes or privilege_findings or reachable_role_findings)
    return {
        "current_user": role.get("current_user"),
        "session_user": role.get("session_user"),
        "role_name": role.get("rolname") or role.get("current_user"),
        "role_type": "writer_or_mutating" if writer_like else "readonly_candidate",
        "transaction_read_only": role.get("transaction_read_only"),
        "unsafe_role_attributes": unsafe_attributes,
        "reachable_role_findings": reachable_role_findings,
        "mutating_privilege_findings": privilege_findings,
    }


def _privilege_findings(permission_probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for target in permission_probes:
        for operation in target.get("operations", []):
            if operation.get("privilege_allowed") is True:
                finding = {
                    "target": target.get("target"),
                    "operation": operation.get("operation"),
                    "reason": operation.get("reason"),
                }
                if operation.get("column_privilege_columns"):
                    finding["columns"] = operation.get("column_privilege_columns")
                if operation.get("sequence_privilege_sequences"):
                    finding["sequences"] = operation.get("sequence_privilege_sequences")
                if operation.get("database_name"):
                    finding["database_name"] = operation.get("database_name")
                if operation.get("role_name"):
                    finding["role_name"] = operation.get("role_name")
                if operation.get("reachable_via"):
                    finding["reachable_via"] = operation.get("reachable_via")
                if operation.get("unsafe_role_attributes"):
                    finding["unsafe_role_attributes"] = operation.get("unsafe_role_attributes")
                if operation.get("mutating_privilege_findings"):
                    finding["mutating_privilege_findings"] = operation.get("mutating_privilege_findings")
                findings.append(finding)
    return findings


def _reachable_role_findings(permission_probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for target in permission_probes:
        if target.get("surface") == "reachable_role_membership":
            return list(target.get("reachable_role_findings", []))
    return []


def _overall_status(
    *,
    role_evidence: Mapping[str, Any],
    permission_probes: list[dict[str, Any]],
    route_smoke: list[dict[str, Any]],
    manual_actions: list[dict[str, Any]],
) -> str:
    if role_evidence.get("role_type") == "writer_or_mutating":
        return STATUS_FAIL
    all_items = [*permission_probes, *route_smoke, *manual_actions]
    if any(item.get("status") == STATUS_FAIL for item in all_items):
        return STATUS_FAIL
    if any(item.get("status") == STATUS_BLOCKED for item in all_items):
        return STATUS_BLOCKED
    return STATUS_PASS


def _permission_summary(permission_probes: list[dict[str, Any]]) -> dict[str, Any]:
    operations = [operation for target in permission_probes for operation in target.get("operations", [])]
    return {
        "target_count": len(permission_probes),
        "operation_count": len(operations),
        "passed_denial_count": sum(1 for operation in operations if operation.get("status") == STATUS_PASS),
        "failed_mutating_count": sum(1 for operation in operations if operation.get("status") == STATUS_FAIL),
        "blocked_count": sum(1 for target in permission_probes if target.get("status") == STATUS_BLOCKED),
    }


def _blocked_summary(
    config: ReadonlyDbValidationConfig,
    *,
    code: str,
    message: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_provenance = provenance or _validation_provenance(
        adapter_injected=False,
        route_requester_injected=False,
        manual_action_probe_runner_injected=False,
    )
    return {
        "schema": _evidence_schema(selected_provenance),
        "status": STATUS_BLOCKED,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(config.database_url),
        "validation_provenance": selected_provenance,
        "validation_timeouts": _validation_timeout_evidence(),
        "blockers": [{"code": code, "message": redact_text(message)}],
        "redaction": {
            "database_url_redacted": True,
            "sensitive_values_redacted": True,
            "evidence_root_approved": True,
        },
    }


def _unexpected_validation_error_summary(
    config: ReadonlyDbValidationConfig,
    *,
    error: BaseException,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    summary = _blocked_summary(
        config,
        code="READONLY_DB_VALIDATION_UNEXPECTED_ERROR",
        message=f"{error.__class__.__name__}: {_safe_db_error_message(error)}",
        provenance=provenance,
    )
    summary["blockers"][0]["error_type"] = error.__class__.__name__
    return summary


def _safe_discover_identity(adapter: ReadonlyDbProbeAdapter) -> dict[str, Any]:
    try:
        return adapter.discover_display_identity()
    except Exception as error:
        return {
            "blockers": [
                {
                    "code": "READONLY_DB_IDENTITY_DISCOVERY_BLOCKED",
                    "reason": redact_text(str(error)),
                }
            ]
        }


def _merged_identity(config: ReadonlyDbValidationConfig, discovered: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(discovered)
    overrides = {
        "source": config.source,
        "cycle_time": config.cycle_time,
        "run_id": config.strict_run_id,
        "model_id": config.model_id,
        "job_id": config.job_id,
    }
    for key, value in overrides.items():
        if value:
            merged[key] = value
    return _json_ready(merged)


def _manual_action_run_id(identity: Mapping[str, Any]) -> str:
    return _identity_text(identity, "run_id") or "readonly-validation-manual-action"


def _identity_text(identity: Mapping[str, Any], key: str) -> str | None:
    value = identity.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _status_from_children(items: list[Mapping[str, Any]]) -> str:
    if any(item.get("status") == STATUS_FAIL for item in items):
        return STATUS_FAIL
    if any(item.get("status") == STATUS_BLOCKED for item in items):
        return STATUS_BLOCKED
    return STATUS_PASS


def _is_permission_denied(error: psycopg2.Error) -> bool:
    code = getattr(error, "pgcode", None)
    message = _safe_db_error_message(error).lower()
    return code in DENIED_SQLSTATES or "permission denied" in message or "read-only transaction" in message


def _safe_db_error_message(error: BaseException) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    return redact_text(text)


def _redact_database_url(database_url: str | None) -> str | None:
    if not database_url:
        return None
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return "[redacted]"
    if not parsed.scheme:
        return redact_text(database_url)
    host = parsed.hostname or ""
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _safe_resolved_evidence_root(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    approved_roots = tuple(root.expanduser().resolve(strict=False) for root in APPROVED_EVIDENCE_ROOTS)
    for root in approved_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ReadonlyDbValidationError(
        "READONLY_DB_EVIDENCE_ROOT_UNAPPROVED",
        "Readonly DB evidence root must be under repository artifacts/ or /scratch/frd_muziyao.",
    )


def _refuse_symlink_components(path: Path) -> None:
    current = path.expanduser()
    candidates = [current, *current.parents]
    for component in candidates:
        if component.exists() and component.is_symlink():
            raise ReadonlyDbValidationError(
                "READONLY_DB_EVIDENCE_PATH_UNSAFE",
                f"Evidence path component must not be a symlink: {component}.",
            )


def _safe_run_id(value: str) -> str:
    text = value.strip()
    if not SAFE_RUN_ID_RE.fullmatch(text) or ".." in text:
        raise ReadonlyDbValidationError(
            "READONLY_DB_RUN_ID_UNSAFE",
            "run_id must be a bounded alphanumeric identifier using only '.', '_' or '-'.",
        )
    return text


def _default_run_id() -> str:
    return f"readonly-db-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _ddl_suffix(run_id: str) -> str:
    suffix = SAFE_DDL_SUFFIX_RE.sub("_", run_id.lower()).strip("_")
    return (suffix or "probe")[:48]


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value) if value else default


def _validation_provenance(
    *,
    adapter_injected: bool,
    route_requester_injected: bool,
    manual_action_probe_runner_injected: bool,
) -> dict[str, Any]:
    injected_components = [
        name
        for name, injected in (
            ("adapter", adapter_injected),
            ("route_requester", route_requester_injected),
            ("manual_action_probe_runner", manual_action_probe_runner_injected),
        )
        if injected
    ]
    return {
        "mode": "simulated" if injected_components else "live",
        "live_readonly_proof": not injected_components,
        "injected_components": injected_components,
    }


def _evidence_schema(provenance: Mapping[str, Any]) -> str:
    return SIMULATED_EVIDENCE_SCHEMA if provenance.get("mode") == "simulated" else LIVE_EVIDENCE_SCHEMA


def _validation_pgoptions() -> str:
    return (
        f"-c statement_timeout={VALIDATION_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={VALIDATION_LOCK_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout={VALIDATION_IDLE_TIMEOUT_MS}"
    )


def _validation_connect_kwargs() -> dict[str, Any]:
    return {
        "connect_timeout": VALIDATION_CONNECT_TIMEOUT_SECONDS,
        "options": _validation_pgoptions(),
    }


def _validation_timeout_evidence() -> dict[str, int]:
    return {
        "connect_timeout_seconds": VALIDATION_CONNECT_TIMEOUT_SECONDS,
        "statement_timeout_ms": VALIDATION_STATEMENT_TIMEOUT_MS,
        "lock_timeout_ms": VALIDATION_LOCK_TIMEOUT_MS,
        "idle_in_transaction_session_timeout_ms": VALIDATION_IDLE_TIMEOUT_MS,
    }


def _bounded_database_url(database_url: str) -> str:
    if not database_url:
        return database_url
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return database_url
    if not parsed.scheme:
        return database_url
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"connect_timeout", "options"}
    ]
    query_items.extend(
        (
            ("connect_timeout", str(VALIDATION_CONNECT_TIMEOUT_SECONDS)),
            ("options", _validation_pgoptions()),
        )
    )
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_items),
            parsed.fragment,
        )
    )


def _display_app_env() -> dict[str, str]:
    return {
        "NHMS_REQUIRE_SERVICE_ROLE": "true",
        "NHMS_SERVICE_ROLE": "display_readonly",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS": "true",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS": "false",
    }


def _display_validation_env(*, database_url: str | None = None) -> dict[str, str | None]:
    env: dict[str, str | None] = {
        **_display_app_env(),
        **_validation_auth_env(),
        "PGOPTIONS": _validation_pgoptions(),
    }
    if database_url is not None:
        env["DATABASE_URL"] = _bounded_database_url(database_url)
    return env


def _validation_auth_env() -> dict[str, str | None]:
    return {
        "ALLOW_DEV_ROLE_HEADER": "true",
        "NHMS_AUTH_MODE": None,
        "AUTH_BACKEND": None,
    }


def _operator_headers() -> dict[str, str]:
    return {
        "X-User-ID": "readonly-db-validation",
        "X-User-Role": "operator",
    }


@contextmanager
def _temporary_env(updates: Mapping[str, str | None]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _public_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate display_readonly database boundary evidence.")
    parser.add_argument("--database-url", help="Explicit real readonly PostgreSQL URL for this validation lane.")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=None,
        help="Root directory for readonly DB evidence bundles.",
    )
    parser.add_argument("--run-id", help="Evidence bundle ID, not the business hydro.hydro_run.run_id.")
    parser.add_argument("--source")
    parser.add_argument("--cycle-time")
    parser.add_argument(
        "--strict-run-id",
        help="Business hydro.hydro_run.run_id override; same meaning as NHMS_READONLY_DB_VALIDATION_RUN_ID.",
    )
    parser.add_argument("--model-id")
    parser.add_argument("--job-id")
    parser.add_argument(
        "--merge-source-dir",
        action="append",
        dest="merge_source_dirs",
        type=Path,
        help="Merge an already-produced per-source readonly DB lane into the current final DB lane. Repeat per source.",
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.merge_source_dirs:
            summary = merge_readonly_db_source_evidence(
                evidence_root=args.evidence_root or DEFAULT_EVIDENCE_ROOT,
                run_id=args.run_id or os.getenv("NHMS_READONLY_DB_VALIDATION_EVIDENCE_RUN_ID") or _default_run_id(),
                source_dirs=args.merge_source_dirs,
                force=args.force,
            )
        else:
            summary = validate_readonly_db_boundary(
                ReadonlyDbValidationConfig.from_env(
                    evidence_root=args.evidence_root,
                    run_id=args.run_id,
                    database_url=args.database_url,
                    source=args.source,
                    cycle_time=args.cycle_time,
                    strict_run_id=args.strict_run_id,
                    model_id=args.model_id,
                    job_id=args.job_id,
                    force=args.force,
                )
            )
    except ReadonlyDbValidationError as error:
        print(f"{error.error_code}: {redact_text(error.message)}", file=sys.stderr)
        return 1
    print(json.dumps(redact_payload(summary), sort_keys=True))
    if summary.get("status") == STATUS_PASS:
        return 0
    if summary.get("status") == STATUS_BLOCKED:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
