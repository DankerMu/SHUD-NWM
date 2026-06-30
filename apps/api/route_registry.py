from fastapi import APIRouter, FastAPI

from apps.api.routes.best_available import router as best_available_router
from apps.api.routes.data_sources import router as data_sources_router
from apps.api.routes.forecast import router as forecast_router
from apps.api.routes.hydro_display import router as hydro_display_router
from apps.api.routes.models import router as models_router
from apps.api.routes.pipeline import router as pipeline_router
from apps.api.routes.state_snapshots import router as state_snapshots_router
from apps.api.runtime_mode import RuntimeConfig
from services.slurm_gateway.routes import router as slurm_router

_BUSINESS_ROUTERS: tuple[APIRouter, ...] = (
    models_router,
    forecast_router,
    best_available_router,
    data_sources_router,
    state_snapshots_router,
    pipeline_router,
    hydro_display_router,
)


def register_role_aware_routes(
    api: FastAPI,
    runtime_config: RuntimeConfig,
    *,
    runtime_router: APIRouter,
) -> None:
    for router in _BUSINESS_ROUTERS:
        api.include_router(router)
    api.include_router(runtime_router)
    if runtime_config.slurm_routes_enabled:
        api.include_router(slurm_router)
