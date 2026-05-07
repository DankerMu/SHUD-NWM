import os

from fastapi import FastAPI

from services.slurm_gateway.routes import router as slurm_router

app = FastAPI(
    title="NHMS API",
    description="National Hydrological Modeling System API",
    version="0.1.0",
)

app.include_router(slurm_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nhms-api",
        "version": "0.1.0",
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host="0.0.0.0", port=port, reload=True)
