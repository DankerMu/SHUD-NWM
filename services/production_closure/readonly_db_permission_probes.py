from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from services.production_closure.readonly_db_types import (
    PERMISSION_PROBE_TARGETS,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PASS,
    TABLE_CATALOG_MUTATING_OPERATIONS,
    TABLE_CATALOG_ONLY_MUTATING_OPERATIONS,
    PermissionProbeSpec,
    ProbeExecution,
    ProbeTarget,
    ReadonlyDbProbeAdapter,
)


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
    operations = [_reachable_role_operation_result(role_finding) for role_finding in reachable_role_privileges]
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
        if sequence.get("mutating_privilege_allowed") or sequence.get("usage") or sequence.get("update")
    ]
    return {
        "allowed": bool(mutating_sequences),
        "reason": (f"{reason_prefix}_mutating_sequence_privilege" if mutating_sequences else None),
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


def _status_from_children(items: list[Mapping[str, Any]]) -> str:
    if any(item.get("status") == STATUS_FAIL for item in items):
        return STATUS_FAIL
    if any(item.get("status") == STATUS_BLOCKED for item in items):
        return STATUS_BLOCKED
    return STATUS_PASS
