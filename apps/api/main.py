import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from apps.api import openapi_patching, route_registry
from apps.api.auth import audit_record, evaluate_request_action
from apps.api.display_cache import start_display_catalog_warmer
from apps.api.errors import error_response, register_error_handlers
from apps.api.runtime_mode import RuntimeConfig, load_runtime_config

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "apps" / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"

_PRE_BODY_PROTECTED_MUTATIONS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("POST", "/api/v1/basins"): ("models.switch_version", "model_registry", "basins"),
    ("POST", "/api/v1/river-networks"): ("models.switch_version", "model_registry", "river-networks"),
    ("POST", "/api/v1/mesh-versions"): ("models.switch_version", "model_registry", "mesh-versions"),
    ("POST", "/api/v1/models"): ("models.switch_version", "model_registry", "models"),
    (
        "POST",
        "/api/v1/river-segment-crosswalks",
    ): ("models.switch_version", "model_registry", "river-segment-crosswalks"),
    ("POST", "/api/v1/hindcast/submit"): ("pipeline.rerun_cycle", "hindcast", "pre-body"),
}
_ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES = 4096
runtime_router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])


async def protected_mutation_auth_guard(request: Any, call_next: Any) -> Any:
    path_policy = await _protected_mutation_policy(request)
    if path_policy is None:
        return await call_next(request)
    if isinstance(path_policy, _PreBodyPolicyError):
        return error_response(
            request,
            status_code=path_policy.status_code,
            code=path_policy.code,
            message=path_policy.message,
            details=path_policy.details,
        )

    request_id = _ensure_request_id(request)
    action_id, target_type, target_id = path_policy
    decision = evaluate_request_action(request, action_id, target_type=target_type, target_id=target_id)
    if decision.decision == "allow":
        return await call_next(request)

    audit = audit_record(decision, request_id=request_id)
    details = {"policy_decision": decision.to_dict(), "audit_record": audit}
    if decision.decision == "release_blocked":
        details["removal_criteria"] = "Configure and prove live backend identity-provider role mapping."
        return error_response(
            request,
            status_code=503,
            code="RELEASE_BLOCKED",
            message=decision.reason,
            details=details,
        )
    if decision.reason_code == "AUTH_REQUIRED":
        return error_response(
            request,
            status_code=401,
            code="AUTH_REQUIRED",
            message=decision.reason,
            details=details,
        )
    return error_response(
        request,
        status_code=403,
        code="RBAC_FORBIDDEN",
        message=decision.reason,
        details=details,
    )


class _PreBodyPolicyError:
    def __init__(self, *, status_code: int, code: str, message: str, details: Any | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class CacheControlStaticFiles(StaticFiles):
    """StaticFiles + 固定 Cache-Control（Vite hash 资产可 immutable 永久缓存）。"""

    def __init__(self, *args: Any, cache_control: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = self._cache_control
        return response


async def _protected_mutation_policy(request: Any) -> tuple[str, str, str] | _PreBodyPolicyError | None:
    method = request.method.upper()
    path = request.url.path
    policy = _PRE_BODY_PROTECTED_MUTATIONS.get((method.upper(), path))
    if policy is not None:
        return policy
    if method == "POST" and path.startswith("/api/v1/basins/") and path.endswith("/versions"):
        basin_id = path.removeprefix("/api/v1/basins/").removesuffix("/versions")
        if basin_id and "/" not in basin_id:
            return ("models.switch_version", "model_registry", basin_id)
    if method == "PUT" and path.startswith("/api/v1/models/") and path.endswith("/active"):
        model_id = path.removeprefix("/api/v1/models/").removesuffix("/active")
        if model_id and "/" not in model_id:
            active = await _active_toggle_flag(request)
            if isinstance(active, _PreBodyPolicyError):
                return active
            action_id = "models.activate" if active else "models.deactivate"
            return (action_id, "model_instance", model_id)
    if (
        method == "POST"
        and path.startswith("/api/v1/models/")
        and (path.endswith("/lifecycle") or path.endswith("/preflight"))
    ):
        suffix = "/lifecycle" if path.endswith("/lifecycle") else "/preflight"
        model_id = path.removeprefix("/api/v1/models/").removesuffix(suffix)
        if model_id and "/" not in model_id:
            operation = await _model_lifecycle_operation_flag(request)
            if isinstance(operation, _PreBodyPolicyError):
                return operation
            action_id = {
                "activate": "models.activate",
                "deactivate": "models.deactivate",
                "switch_version": "models.switch_version",
                "rollback_version": "models.rollback_version",
                "supersede": "models.supersede",
                "deprecate": "models.deactivate",
            }[operation]
            return (action_id, "model_instance", model_id)
    return None


async def _active_toggle_flag(request: Any) -> bool | _PreBodyPolicyError:
    request_id = _ensure_request_id(request)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
            return _active_toggle_validation_error(request_id)
    except ValueError:
        return _active_toggle_validation_error(request_id)

    body = await _read_bounded_active_toggle_body(request)
    if body is None:
        return _active_toggle_validation_error(request_id)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _active_toggle_validation_error(request_id)
    if not isinstance(payload, dict):
        return _active_toggle_validation_error(request_id)
    active = payload.get("active", payload.get("active_flag"))
    if not isinstance(active, bool):
        return _active_toggle_validation_error(request_id)
    return active


async def _read_bounded_active_toggle_body(request: Any) -> bytes | None:
    chunks: list[bytes] = []
    buffered = 0
    max_with_sentinel = _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES + 1

    while True:
        message = await request._receive()
        if message.get("type") == "http.disconnect":
            return None

        chunk = message.get("body", b"")
        if chunk:
            remaining = max_with_sentinel - buffered
            if remaining > 0:
                chunks.append(chunk[:remaining])
                buffered += min(len(chunk), remaining)
            if buffered > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
                return None

        if not message.get("more_body", False):
            break

    return b"".join(chunks)


def _active_toggle_validation_error(request_id: str) -> _PreBodyPolicyError:
    return _PreBodyPolicyError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[
            {
                "field": "body.active",
                "rejected_value": None,
                "reason": (
                    "Active-toggle requests require a bounded JSON object with boolean active "
                    "or active_flag before authorization can be evaluated."
                ),
                "request_id": request_id,
            }
        ],
    )


async def _model_lifecycle_operation_flag(request: Any) -> str | _PreBodyPolicyError:
    request_id = _ensure_request_id(request)
    content_length = request.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES:
            return _model_lifecycle_validation_error(request_id)
    except ValueError:
        return _model_lifecycle_validation_error(request_id)

    body = await _read_bounded_active_toggle_body(request)
    if body is None:
        return _model_lifecycle_validation_error(request_id)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _model_lifecycle_validation_error(request_id)
    if not isinstance(payload, dict):
        return _model_lifecycle_validation_error(request_id)
    operation = payload.get("operation")
    if operation not in {"activate", "deactivate", "switch_version", "rollback_version", "supersede", "deprecate"}:
        return _model_lifecycle_validation_error(request_id)
    return str(operation)


def _model_lifecycle_validation_error(request_id: str) -> _PreBodyPolicyError:
    return _PreBodyPolicyError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed.",
        details=[
            {
                "field": "body.operation",
                "rejected_value": None,
                "reason": "Model lifecycle requests require a bounded JSON object with a supported operation.",
                "request_id": request_id,
            }
        ],
    )


def _ensure_request_id(request: Any) -> str:
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        return request_id
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    return request_id


@runtime_router.get("/config")
def runtime_config(request: Request) -> dict[str, Any]:
    config: RuntimeConfig = request.app.state.runtime_config
    return _ok(request, config.public_dict())


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    runtime_config = load_runtime_config(env)
    api = FastAPI(
        title="NHMS API",
        description="National Hydrological Modeling System API",
        version="0.1.0",
    )
    api.state.runtime_config = runtime_config
    api.state.object_store_root = runtime_config.object_store_root

    register_error_handlers(api)
    api.middleware("http")(protected_mutation_auth_guard)
    # 传输层 gzip：河段 GeoJSON / 静态河网 / 前端 dist 资产以 MB 计，明文传输是显示端
    # 首屏与全河段加载慢的主因之一（实测 842KB GeoJSON gzip 后 ~150KB）。
    api.add_middleware(GZipMiddleware, minimum_size=1024)

    route_registry.register_role_aware_routes(api, runtime_config, runtime_router=runtime_router)

    _register_static_and_health_routes(api)
    api.openapi = _custom_openapi_factory(api)
    if runtime_config.display_readonly:
        # 目录缓存自预热：保持热 key 常新，访客稳态不踩只读副本 12s 级慢查询。
        start_display_catalog_warmer(api)
    return api


def _custom_openapi_factory(api: FastAPI) -> Any:
    return openapi_patching.custom_openapi_factory(api, patch_schema=_patch_openapi_schema)


def custom_openapi() -> dict[str, Any]:
    return _custom_openapi_factory(app)()


def _patch_openapi_schema(schema: dict) -> None:
    _patch_mvt_tile_openapi(schema)
    _patch_flood_duration_openapi(schema)
    _patch_flood_product_quality_openapi(schema)
    _patch_station_series_openapi(schema)
    _patch_qhh_latest_product_openapi(schema)
    _patch_met_stations_list_openapi(schema)
    _patch_layer_metadata_openapi(schema)
    _patch_pipeline_openapi(schema)
    _patch_runtime_openapi(schema)


def __getattr__(name: str) -> Any:
    if name.startswith("_") and hasattr(openapi_patching, name):
        return getattr(openapi_patching, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_patch_mvt_tile_openapi = openapi_patching._patch_mvt_tile_openapi
_patch_flood_duration_openapi = openapi_patching._patch_flood_duration_openapi
_patch_flood_product_quality_openapi = openapi_patching._patch_flood_product_quality_openapi
_patch_station_series_openapi = openapi_patching._patch_station_series_openapi
_patch_qhh_latest_product_openapi = openapi_patching._patch_qhh_latest_product_openapi
_patch_met_stations_list_openapi = openapi_patching._patch_met_stations_list_openapi
_patch_layer_metadata_openapi = openapi_patching._patch_layer_metadata_openapi
_patch_pipeline_openapi = openapi_patching._patch_pipeline_openapi
_patch_runtime_openapi = openapi_patching._patch_runtime_openapi


def _register_static_and_health_routes(api: FastAPI) -> None:
    @api.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "nhms-api",
            "version": "0.1.0",
        }

    api.mount(
        "/assets",
        # Vite 产物文件名带内容 hash：内容不变 URL 不变 → 可永久缓存（immutable）。
        CacheControlStaticFiles(
            directory=FRONTEND_DIST_DIR / "assets",
            check_dir=False,
            cache_control="public, max-age=31536000, immutable",
        ),
        name="frontend-assets",
    )

    @api.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        # Serve real built static files outside /assets (e.g. /geo/*.geojson, favicon)
        # before falling back to index.html for SPA client routes. Reject path
        # traversal by confirming the resolved path stays inside the dist root.
        if full_path:
            candidate = (FRONTEND_DIST_DIR / full_path).resolve()
            try:
                candidate.relative_to(FRONTEND_DIST_DIR.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                # 非 hash 命名的静态文件（/geo/*.geojson、favicon）：短缓存 + 必须重验，
                # 部署新文件后客户端最迟一次条件请求即拿到新版。
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        # index.html 绝不能被启发式缓存：旧 index 引用旧 hash bundle，会让用户在
        # 部署后长期跑旧前端（实测导致总览相机/河网修复"看不到"）。no-cache 仍允许
        # ETag/Last-Modified 条件请求 304，代价只是每次一个轻量 revalidate。
        return FileResponse(FRONTEND_INDEX, headers={"Cache-Control": "no-cache"})


def _ok(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
