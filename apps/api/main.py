import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from apps.api.errors import register_error_handlers
from apps.api.routes.data_sources import router as data_sources_router
from apps.api.routes.forecast import router as forecast_router
from apps.api.routes.models import router as models_router
from services.slurm_gateway.routes import router as slurm_router

app = FastAPI(
    title="NHMS API",
    description="National Hydrological Modeling System API",
    version="0.1.0",
)

register_error_handlers(app)
app.include_router(models_router)
app.include_router(forecast_router)
app.include_router(data_sources_router)
app.include_router(slurm_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nhms-api",
        "version": "0.1.0",
    }


app.mount("/", StaticFiles(directory="apps/frontend", html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
