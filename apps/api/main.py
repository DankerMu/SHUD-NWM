import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from apps.api.errors import register_error_handlers
from apps.api.routes.best_available import router as best_available_router
from apps.api.routes.data_sources import router as data_sources_router
from apps.api.routes.forecast import router as forecast_router
from apps.api.routes.models import router as models_router
from apps.api.routes.pipeline import router as pipeline_router
from apps.api.routes.state_snapshots import router as state_snapshots_router
from services.slurm_gateway.routes import router as slurm_router

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "apps" / "frontend" / "dist"
FRONTEND_INDEX = FRONTEND_DIST_DIR / "index.html"

app = FastAPI(
    title="NHMS API",
    description="National Hydrological Modeling System API",
    version="0.1.0",
)

register_error_handlers(app)
app.include_router(models_router)
app.include_router(forecast_router)
app.include_router(best_available_router)
app.include_router(data_sources_router)
app.include_router(state_snapshots_router)
app.include_router(pipeline_router)
app.include_router(slurm_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nhms-api",
        "version": "0.1.0",
    }


app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_DIST_DIR / "assets", check_dir=False),
    name="frontend-assets",
)


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
