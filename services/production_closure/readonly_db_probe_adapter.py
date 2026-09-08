from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from packages.common.redaction import redact_text
from services.production_closure.readonly_db_permission_probes import (
    _database_create_catalog_finding,
    _schema_sequence_catalog_findings,
    _sequence_privilege_rows,
    _target_catalog_findings,
)
from services.production_closure.readonly_db_types import (
    DENIED_SQLSTATES,
    ROLE_ATTRIBUTE_WRITE_FLAGS,
    VALIDATION_CONNECT_TIMEOUT_SECONDS,
    VALIDATION_IDLE_TIMEOUT_MS,
    VALIDATION_LOCK_TIMEOUT_MS,
    VALIDATION_STATEMENT_TIMEOUT_MS,
    PermissionProbeSpec,
    ProbeExecution,
    ProbeTarget,
    ReadonlyDbValidationError,
)


class PsycopgReadonlyDbProbeAdapter:
    def __init__(self, database_url: str, *, ddl_suffix: str, connect_fn: Any | None = None) -> None:
        self.database_url = database_url
        self.ddl_suffix = ddl_suffix
        self._connect_fn = connect_fn

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
        inherit_option = "COALESCE(m.inherit_option, true)" if "inherit_option" in membership_columns else "true"
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
        unsafe_attributes = {flag: bool(role.get(flag)) for flag in ROLE_ATTRIBUTE_WRITE_FLAGS if bool(role.get(flag))}
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
        connection = self._connect()(self.database_url, **_validation_connect_kwargs())
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


    def _connect(self) -> Any:
        if self._connect_fn is not None:
            return self._connect_fn
        import psycopg2 as _psycopg2

        return _psycopg2.connect

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
        connection = self._connect()(
            self.database_url,
            cursor_factory=RealDictCursor,
            **_validation_connect_kwargs(),
        )
        try:
            yield connection
            connection.rollback()
        finally:
            connection.close()


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


def _safe_db_error_message(error: BaseException) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    return redact_text(text)


def _is_permission_denied(error: psycopg2.Error) -> bool:
    code = getattr(error, "pgcode", None)
    message = _safe_db_error_message(error).lower()
    return code in DENIED_SQLSTATES or "permission denied" in message or "read-only transaction" in message


def _validation_connect_kwargs() -> dict[str, Any]:
    return {
        "connect_timeout": VALIDATION_CONNECT_TIMEOUT_SECONDS,
        "options": _validation_pgoptions(),
    }


def _validation_pgoptions() -> str:
    return (
        f"-c statement_timeout={VALIDATION_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={VALIDATION_LOCK_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout={VALIDATION_IDLE_TIMEOUT_MS}"
    )
