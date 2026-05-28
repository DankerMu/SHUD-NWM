from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from packages.common.redaction import redact_payload, redact_text
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "two-node-e2e"
APPROVED_EVIDENCE_ROOTS = (REPO_ROOT / "artifacts", Path("/scratch/frd_muziyao"))
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_DDL_SUFFIX_RE = re.compile(r"[^a-z0-9_]+")

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

READONLY_DB_URL_ENVS = (
    "NHMS_DISPLAY_READONLY_DATABASE_URL",
    "NHMS_READONLY_DB_VALIDATION_DATABASE_URL",
)
VALIDATION_ENV_PREFIX = "NHMS_READONLY_DB_VALIDATION_"
DENIED_SQLSTATES = frozenset({"25006", "42501"})
BLOCKED_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
ROLE_ATTRIBUTE_WRITE_FLAGS = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
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
    update_column: str | None = None
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

    def schema_privileges(self, schema: str) -> dict[str, bool]:
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
                        has_table_privilege(current_user, %s, 'DELETE') AS delete
                    """,
                    (target.qualified_name, target.qualified_name, target.qualified_name),
                )
                return {key: bool(value) for key, value in dict(cursor.fetchone()).items()}

    def schema_privileges(self, schema: str) -> dict[str, bool]:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'CREATE') AS create",
                    (schema,),
                )
                return {key: bool(value) for key, value in dict(cursor.fetchone()).items()}

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
                    ORDER BY ordinal_position
                    LIMIT 1
                    """,
                    (target.schema, target.table),
                )
                row = cursor.fetchone()
        return str(row["column_name"]) if row else None

    def execute_probe(self, spec: PermissionProbeSpec) -> ProbeExecution:
        connection = psycopg2.connect(self.database_url)
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
        if spec.operation == "INSERT" and spec.target is not None:
            return sql.SQL("INSERT INTO {}.{} DEFAULT VALUES").format(
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
            )
        if spec.operation == "UPDATE" and spec.target is not None and spec.update_column is not None:
            return sql.SQL("UPDATE {}.{} SET {} = {} WHERE FALSE").format(
                sql.Identifier(spec.target.schema),
                sql.Identifier(spec.target.table),
                sql.Identifier(spec.update_column),
                sql.Identifier(spec.update_column),
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
        connection = psycopg2.connect(self.database_url, cursor_factory=RealDictCursor)
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
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=config.force)
    writer.prepare()

    if not config.database_url:
        summary = _blocked_summary(
            config,
            code="READONLY_DB_URL_MISSING",
            message=(
                "A real readonly database URL is required via NHMS_DISPLAY_READONLY_DATABASE_URL, "
                "NHMS_READONLY_DB_VALIDATION_DATABASE_URL, or --database-url."
            ),
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
    summary = {
        "schema": "nhms.readonly_db_boundary.evidence.v1",
        "status": status,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(database_url),
        "source_env_vars": list(READONLY_DB_URL_ENVS),
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
    results = [_table_probe_result(adapter, target) for target in PERMISSION_PROBE_TARGETS]
    results.append(_ddl_probe_result(adapter, ddl_suffix=ddl_suffix))
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

    app = create_app(_display_app_env())
    app.dependency_overrides[pipeline_routes.get_pipeline_store] = forbidden_dependency
    app.dependency_overrides[pipeline_routes.get_retry_service] = forbidden_dependency
    app.dependency_overrides[pipeline_routes.get_slurm_gateway] = forbidden_dependency
    results: list[dict[str, Any]] = []
    validation_env = _validation_auth_env()
    if database_url:
        validation_env["DATABASE_URL"] = database_url
    with _temporary_env(validation_env):
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


def _table_probe_result(adapter: ReadonlyDbProbeAdapter, target: ProbeTarget) -> dict[str, Any]:
    if not adapter.table_exists(target):
        return {
            "target": target.qualified_name,
            "surface": target.surface,
            "status": STATUS_BLOCKED,
            "reason": "required_table_absent_in_fixture",
            "operations": [],
        }
    privileges = adapter.table_privileges(target)
    update_column = adapter.first_updatable_column(target)
    operations = [
        _dml_operation_result(
            adapter,
            PermissionProbeSpec(
                operation="INSERT",
                target=target,
                command=f"INSERT INTO {target.qualified_name} DEFAULT VALUES",
            ),
            privilege_allowed=privileges.get("insert", False),
        )
    ]
    if update_column is None:
        operations.append(
            {
                "operation": "UPDATE",
                "command": f"UPDATE {target.qualified_name} SET <column> = <column> WHERE FALSE",
                "status": STATUS_BLOCKED,
                "reason": "no_updatable_column_available",
            }
        )
    else:
        operations.append(
            _dml_operation_result(
                adapter,
                PermissionProbeSpec(
                    operation="UPDATE",
                    target=target,
                    update_column=update_column,
                    command=f"UPDATE {target.qualified_name} SET {update_column} = {update_column} WHERE FALSE",
                ),
                privilege_allowed=privileges.get("update", False),
            )
        )
    operations.append(
        _dml_operation_result(
            adapter,
            PermissionProbeSpec(
                operation="DELETE",
                target=target,
                command=f"DELETE FROM {target.qualified_name} WHERE FALSE",
            ),
            privilege_allowed=privileges.get("delete", False),
        )
    )
    return {
        "target": target.qualified_name,
        "surface": target.surface,
        "status": _status_from_children(operations),
        "table_privileges": privileges,
        "operations": operations,
    }


def _dml_operation_result(
    adapter: ReadonlyDbProbeAdapter,
    spec: PermissionProbeSpec,
    *,
    privilege_allowed: bool,
) -> dict[str, Any]:
    execution = adapter.execute_probe(spec)
    status = _operation_status(execution, privilege_allowed=privilege_allowed)
    result = {
        "operation": spec.operation,
        "command": spec.command,
        "status": status,
        "privilege_allowed": privilege_allowed,
        "execution_outcome": execution.outcome,
        "sqlstate": execution.sqlstate,
        "rolled_back": execution.rolled_back,
    }
    if execution.message:
        result["message"] = execution.message
    if execution.rowcount is not None:
        result["rowcount"] = execution.rowcount
    if privilege_allowed:
        result["reason"] = "tested_credential_has_mutating_table_privilege"
    elif execution.outcome == "succeeded":
        result["reason"] = "mutating_probe_executed_successfully_before_rollback"
    elif execution.outcome == "denied":
        result["reason"] = "mutating_probe_denied_before_commit"
    else:
        result["reason"] = "mutating_probe_blocked_by_fixture_or_unexpected_database_error"
    return result


def _ddl_probe_result(adapter: ReadonlyDbProbeAdapter, *, ddl_suffix: str) -> dict[str, Any]:
    schema_name = "ops"
    probe_table = f"__nhms_readonly_validation_probe_{ddl_suffix}"
    command = f"CREATE TABLE {schema_name}.{probe_table} (id integer)"
    if not adapter.schema_exists(schema_name):
        return {
            "target": f"{schema_name}.*",
            "surface": "schema_table_ddl",
            "status": STATUS_BLOCKED,
            "reason": "required_schema_absent_in_fixture",
            "operations": [],
        }
    privileges = adapter.schema_privileges(schema_name)
    execution = adapter.execute_probe(
        PermissionProbeSpec(
            operation="DDL_CREATE_TABLE",
            target=None,
            ddl_schema=schema_name,
            ddl_table=probe_table,
            command=command,
        )
    )
    privilege_allowed = privileges.get("create", False)
    operation = {
        "operation": "DDL_CREATE_TABLE",
        "command": command,
        "status": _operation_status(execution, privilege_allowed=privilege_allowed),
        "privilege_allowed": privilege_allowed,
        "execution_outcome": execution.outcome,
        "sqlstate": execution.sqlstate,
        "rolled_back": execution.rolled_back,
        "reason": (
            "tested_credential_has_schema_create_privilege"
            if privilege_allowed
            else "ddl_probe_executed_successfully_before_rollback"
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
    source = _identity_text(identity, "source") or "GFS"
    cycle_time = _identity_text(identity, "cycle_time")
    run_id = _identity_text(identity, "run_id")
    model_id = _identity_text(identity, "model_id") or "basins_qhh_shud"
    job_id = _identity_text(identity, "job_id")
    latest_path = f"/api/v1/mvp/qhh/latest-product?source={_url_value(source)}"
    if run_id and cycle_time and model_id:
        latest_path = (
            f"/api/v1/mvp/qhh/latest-product?source={_url_value(source)}"
            f"&cycle_time={_url_value(cycle_time)}&run_id={_url_value(run_id)}&model_id={_url_value(model_id)}"
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
        {"name": "latest_product", "method": "GET", "path": latest_path, "fixture_blocker_allowed": True},
        {"name": "jobs", "method": "GET", "path": "/api/v1/jobs?limit=1"},
    ]
    if source and cycle_time:
        routes.extend(
            [
                {
                    "name": "pipeline_status",
                    "method": "GET",
                    "path": f"/api/v1/pipeline/status?source={_url_value(source)}&cycle_time={_url_value(cycle_time)}",
                    "fixture_blocker_allowed": True,
                },
                {
                    "name": "pipeline_stages",
                    "method": "GET",
                    "path": f"/api/v1/pipeline/stages?source={_url_value(source)}&cycle_time={_url_value(cycle_time)}",
                    "fixture_blocker_allowed": True,
                },
            ]
        )
    else:
        routes.extend(
            [
                {
                    "name": "pipeline_status",
                    "method": "GET",
                    "path": None,
                    "status": STATUS_BLOCKED,
                    "reason": "source_and_cycle_time_required_for_pipeline_status_smoke",
                },
                {
                    "name": "pipeline_stages",
                    "method": "GET",
                    "path": None,
                    "status": STATUS_BLOCKED,
                    "reason": "source_and_cycle_time_required_for_pipeline_stages_smoke",
                },
            ]
        )
    if job_id:
        routes.append(
            {
                "name": "job_logs",
                "method": "GET",
                "path": f"/api/v1/jobs/{_url_value(job_id)}/logs",
                "fixture_blocker_allowed": True,
            }
        )
    else:
        routes.append(
            {
                "name": "job_logs",
                "method": "GET",
                "path": None,
                "status": STATUS_BLOCKED,
                "reason": "job_id_with_published_log_required_for_job_log_smoke",
            }
        )
    return routes


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
    code = str(error.get("code") or "")
    return status_code == 404 or code in {
        "QHH_LATEST_PRODUCT_UNAVAILABLE",
        "PIPELINE_CYCLE_NOT_FOUND",
        "PIPELINE_STRICT_IDENTITY_NOT_FOUND",
        "JOB_NOT_FOUND",
        "JOB_LOG_NOT_PUBLISHED",
        "JOB_LOG_NOT_FOUND",
        "JOB_LOG_URI_UNSUPPORTED",
        "JOB_LOG_ACCESS_DENIED",
    }


def _url_value(value: str) -> str:
    return quote(value, safe="")


@contextmanager
def _fastapi_display_route_requester(database_url: str) -> Iterator[RouteRequester]:
    from fastapi.testclient import TestClient

    from apps.api.main import create_app

    app = create_app(_display_app_env())
    with _temporary_env({"DATABASE_URL": database_url, **_validation_auth_env()}):
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
    writer_like = bool(unsafe_attributes or privilege_findings)
    return {
        "current_user": role.get("current_user"),
        "session_user": role.get("session_user"),
        "role_name": role.get("rolname") or role.get("current_user"),
        "role_type": "writer_or_mutating" if writer_like else "readonly_candidate",
        "transaction_read_only": role.get("transaction_read_only"),
        "unsafe_role_attributes": unsafe_attributes,
        "mutating_privilege_findings": privilege_findings,
    }


def _privilege_findings(permission_probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for target in permission_probes:
        for operation in target.get("operations", []):
            if operation.get("privilege_allowed") is True:
                findings.append(
                    {
                        "target": target.get("target"),
                        "operation": operation.get("operation"),
                        "reason": operation.get("reason"),
                    }
                )
    return findings


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


def _blocked_summary(config: ReadonlyDbValidationConfig, *, code: str, message: str) -> dict[str, Any]:
    return {
        "schema": "nhms.readonly_db_boundary.evidence.v1",
        "status": STATUS_BLOCKED,
        "run_id": config.run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "evidence_dir": _public_path(config.lane_dir),
        "database_url": _redact_database_url(config.database_url),
        "blockers": [{"code": code, "message": redact_text(message)}],
        "redaction": {
            "database_url_redacted": True,
            "sensitive_values_redacted": True,
            "evidence_root_approved": True,
        },
    }


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


def _display_app_env() -> dict[str, str]:
    return {
        "NHMS_REQUIRE_SERVICE_ROLE": "true",
        "NHMS_SERVICE_ROLE": "display_readonly",
        "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS": "true",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS": "false",
    }


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
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
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
