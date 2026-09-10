from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2

from packages.common.redaction import redact_payload, redact_text
from services.production_closure.readonly_db_merge import (
    EvidenceWriter,
    _permission_summary,
    _public_path,
    _redact_database_url,
    _validation_timeout_evidence,
    merge_readonly_db_source_evidence,
)
from services.production_closure.readonly_db_permission_probes import run_permission_probe_matrix
from services.production_closure.readonly_db_probe_adapter import (
    PsycopgReadonlyDbProbeAdapter as _PsycopgReadonlyDbProbeAdapter,
)
from services.production_closure.readonly_db_route_smoke import (
    _bounded_database_url as _bounded_database_url,
)
from services.production_closure.readonly_db_route_smoke import (
    _display_app_env as _display_app_env,
)
from services.production_closure.readonly_db_route_smoke import (
    _display_read_routes,
    _route_result,
)
from services.production_closure.readonly_db_route_smoke import (
    _display_validation_env as _display_validation_env,
)
from services.production_closure.readonly_db_route_smoke import (
    _operator_headers as _operator_headers,
)
from services.production_closure.readonly_db_route_smoke import (
    _response_body as _response_body,
)
from services.production_closure.readonly_db_route_smoke import (
    _temporary_env as _temporary_env,
)
from services.production_closure.readonly_db_types import (
    APPROVED_EVIDENCE_ROOTS as APPROVED_EVIDENCE_ROOTS,
)
from services.production_closure.readonly_db_types import (
    AUTHORITATIVE_EVIDENCE_FILENAMES,
    DEFAULT_API_PROBE_ADAPTER_MODULE,
    DEFAULT_EVIDENCE_ROOT,
    LIVE_EVIDENCE_SCHEMA,
    READONLY_DB_URL_ENVS,
    ROLE_ATTRIBUTE_WRITE_FLAGS,
    SAFE_DDL_SUFFIX_RE,
    SIMULATED_EVIDENCE_SCHEMA,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PASS,
    ReadonlyDbProbeAdapter,
    ReadonlyDbValidationConfig,
    ReadonlyDbValidationError,
    RouteRequester,
    _default_run_id,
    _safe_resolved_evidence_root,
)
from services.production_closure.readonly_db_types import (
    PERMISSION_PROBE_TARGETS as PERMISSION_PROBE_TARGETS,
)
from services.production_closure.readonly_db_types import (
    PermissionProbeSpec as PermissionProbeSpec,
)
from services.production_closure.readonly_db_types import (
    ProbeExecution as ProbeExecution,
)
from services.production_closure.readonly_db_types import (
    ProbeTarget as ProbeTarget,
)
from services.production_closure.readonly_db_types import (
    RouteHttpResponse as RouteHttpResponse,
)


class PsycopgReadonlyDbProbeAdapter(_PsycopgReadonlyDbProbeAdapter):
    def __init__(self, database_url: str, *, ddl_suffix: str, connect_fn: Any | None = None) -> None:
        super().__init__(database_url, ddl_suffix=ddl_suffix, connect_fn=connect_fn or psycopg2.connect)


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
        if isinstance(error, ReadonlyDbValidationError) and error.error_code.startswith("READONLY_DB_EVIDENCE_"):
            raise
        summary = _unexpected_validation_error_summary(config, error=error, provenance=provenance)
        writer.write_json(config.lane_dir / "summary.json", summary)
        return redact_payload(summary)


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
    adapter = _default_api_probe_adapter()
    runner = getattr(adapter, "run_manual_action_probes")
    return runner(run_id, database_url=database_url)


@contextmanager
def _fastapi_display_route_requester(database_url: str) -> Iterator[RouteRequester]:
    adapter = _default_api_probe_adapter()
    with adapter.display_route_requester(database_url) as requester:
        yield requester


def _default_api_probe_adapter() -> Any:
    return importlib.import_module(DEFAULT_API_PROBE_ADAPTER_MODULE)


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


def _safe_db_error_message(error: BaseException) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    return redact_text(text)


def _ddl_suffix(run_id: str) -> str:
    suffix = SAFE_DDL_SUFFIX_RE.sub("_", run_id.lower()).strip("_")
    return (suffix or "probe")[:48]


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
    parser.add_argument(
        "--merge-declared-source",
        action="append",
        dest="merge_declared_sources",
        help="Declared source scope for --merge-source-dir. Repeat per source; defaults to full GFS+IFS.",
    )
    parser.add_argument(
        "--reduced-scope",
        action="store_true",
        help="Declare an intentional reduced-scope readonly DB merge; final evidence can feed PARTIAL, not full PASS.",
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
                declared_sources=args.merge_declared_sources,
                reduced_scope=args.reduced_scope,
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
