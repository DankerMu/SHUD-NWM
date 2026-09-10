from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from packages.common.redaction import redact_text
from services.production_closure.readonly_db_types import (
    DISPLAY_OBJECT_STORE_ROOT_ENVS,
    ROUTE_FIXTURE_BLOCKER_ERROR_CODES,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PASS,
    VALIDATION_CONNECT_TIMEOUT_SECONDS,
    VALIDATION_IDLE_TIMEOUT_MS,
    VALIDATION_LOCK_TIMEOUT_MS,
    VALIDATION_STATEMENT_TIMEOUT_MS,
    ReadonlyDbValidationError,
    RouteRequester,
)


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
    return {field: str(value) for field, value in values.items()}, []


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
    response_identity = _route_response_identity(str(spec["name"]), body)
    identity_blockers = _route_response_identity_blockers(spec, response_identity)
    if 200 <= response.status_code < 300 and not identity_blockers:
        status = STATUS_PASS
        reason = "display_read_route_succeeded"
    elif 200 <= response.status_code < 300:
        status = STATUS_BLOCKED
        reason = "display_read_route_response_identity_invalid"
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
    if response_identity:
        result["response_identity"] = response_identity
    if identity_blockers:
        result["identity_blockers"] = identity_blockers
    return result


def _route_response_identity(route_name: str, body: Mapping[str, Any]) -> dict[str, str]:
    required_fields = _route_response_identity_required_fields(route_name)
    if not required_fields:
        return {}
    candidates: list[Any] = [body.get("identity"), body.get("strict_identity")]
    data = body.get("data")
    if isinstance(data, Mapping):
        candidates.extend((data.get("identity"), data.get("strict_identity"), data))
        for key in ("item", "latest_product", "status", "job", "log", "metadata"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                candidates.extend((nested.get("identity"), nested.get("strict_identity"), nested))
        for key in ("items", "jobs", "stages", "logs"):
            nested_list = data.get(key)
            if isinstance(nested_list, list):
                candidates.extend(item for item in nested_list if isinstance(item, Mapping))
    elif isinstance(data, list):
        candidates.extend(item for item in data if isinstance(item, Mapping))

    fields = ("source", "source_id", "cycle_time", "run_id", "model_id", "job_id")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        identity: dict[str, str] = {}
        for identity_field in fields:
            value = candidate.get(identity_field)
            if value is not None and str(value).strip():
                identity[identity_field] = str(value).strip()
        if "source" not in identity and "source_id" in identity:
            identity["source"] = identity["source_id"]
        if all(identity.get(field) for field in required_fields):
            return identity
    return {}


def _route_response_identity_required_fields(route_name: str) -> tuple[str, ...]:
    if route_name == "job_logs":
        return ("source", "cycle_time", "run_id", "model_id", "job_id")
    if route_name in {"latest_product", "pipeline_status", "pipeline_stages", "jobs"}:
        return ("source", "cycle_time", "run_id", "model_id")
    return ()


def _route_response_identity_blockers(
    spec: Mapping[str, Any],
    response_identity: Mapping[str, str],
) -> list[dict[str, Any]]:
    expected = spec.get("strict_identity")
    if not isinstance(expected, Mapping):
        return []
    required_fields = ["source", "cycle_time", "run_id", "model_id"]
    if spec.get("name") == "job_logs":
        required_fields.append("job_id")
    blockers: list[dict[str, Any]] = []
    for identity_field in required_fields:
        expected_value = _identity_text(expected, identity_field)
        observed = response_identity.get(identity_field)
        if identity_field == "job_id" and not observed:
            parsed = urlsplit(str(spec.get("path") or ""))
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 4 and parts[-1] == "logs":
                expected_value = expected_value or parts[-2]
        if not observed:
            blockers.append(
                {
                    "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISSING",
                    "field": identity_field,
                    "expected": expected_value,
                }
            )
            continue
        if identity_field == "source":
            matches = observed.upper() == str(expected_value or "").upper()
        else:
            matches = str(observed) == str(expected_value)
        if not matches:
            blockers.append(
                {
                    "code": "READONLY_DB_ROUTE_RESPONSE_IDENTITY_MISMATCH",
                    "field": identity_field,
                    "expected": expected_value,
                    "observed": observed,
                }
            )
    return blockers


def _route_fixture_blocked(status_code: int, error: Mapping[str, Any]) -> bool:
    del status_code
    code = str(error.get("code") or "")
    return code in ROUTE_FIXTURE_BLOCKER_ERROR_CODES


def _url_value(value: str) -> str:
    return quote(value, safe="")


def _response_body(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"text": response.text}


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
        "OBJECT_STORE_ROOT": _display_object_store_root(),
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


def _display_object_store_root() -> str:
    for env_var in DISPLAY_OBJECT_STORE_ROOT_ENVS:
        raw_value = os.environ.get(env_var, "").strip()
        if raw_value:
            return str(Path(raw_value).expanduser())
    fallback_root = Path(tempfile.gettempdir()).expanduser() / "nhms-display-readonly-object-store"
    try:
        fallback_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReadonlyDbValidationError(
            "DISPLAY_OBJECT_STORE_ROOT_UNAVAILABLE",
            f"Failed to prepare display readonly object-store root {fallback_root}: {error}",
        ) from error
    return str(fallback_root)


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


def _validation_pgoptions() -> str:
    return (
        f"-c statement_timeout={VALIDATION_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={VALIDATION_LOCK_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout={VALIDATION_IDLE_TIMEOUT_MS}"
    )


def _identity_text(identity: Mapping[str, Any], key: str) -> str | None:
    value = identity.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None
