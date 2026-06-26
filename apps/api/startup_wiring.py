from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.display_cache import start_display_catalog_warmer
from apps.api.runtime_mode import RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "apps" / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"


class CacheControlStaticFiles(StaticFiles):
    """StaticFiles + 固定 Cache-Control（Vite hash 资产可 immutable 永久缓存）。"""

    def __init__(self, *args: Any, cache_control: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cache_control = cache_control

    def file_response(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = self._cache_control
        return response


def configure_app_state(api: FastAPI, runtime_config: RuntimeConfig) -> None:
    api.state.runtime_config = runtime_config
    api.state.object_store_root = runtime_config.object_store_root


def create_runtime_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])
    router.add_api_route("/config", runtime_config, methods=["GET"])
    return router


def runtime_config(request: Request) -> dict[str, Any]:
    config: RuntimeConfig = request.app.state.runtime_config
    return ok_response(request, config.public_dict())


def register_static_and_health_routes(
    api: FastAPI,
    *,
    frontend_dist_dir: Path = FRONTEND_DIST_DIR,
    frontend_index: Path = FRONTEND_INDEX,
    static_files_cls: type[CacheControlStaticFiles] = CacheControlStaticFiles,
) -> None:
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
        static_files_cls(
            directory=frontend_dist_dir / "assets",
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
            candidate = (frontend_dist_dir / full_path).resolve()
            try:
                candidate.relative_to(frontend_dist_dir.resolve())
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                # 非 hash 命名的静态文件（/geo/*.geojson、favicon）：短缓存 + 必须重验，
                # 部署新文件后客户端最迟一次条件请求即拿到新版。
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        # index.html 绝不能被启发式缓存：旧 index 引用旧 hash bundle，会让用户在
        # 部署后长期跑旧前端（实测导致总览相机/河网修复"看不到"）。no-cache 仍允许
        # ETag/Last-Modified 条件请求 304，代价只是每次一个轻量 revalidate。
        return FileResponse(frontend_index, headers={"Cache-Control": "no-cache"})


def start_display_cache_warmer_if_needed(api: FastAPI, runtime_config: RuntimeConfig) -> None:
    if runtime_config.display_readonly:
        # 目录缓存自预热：保持热 key 常新，访客稳态不踩只读副本 12s 级慢查询。
        start_display_catalog_warmer(api)


def ok_response(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }
