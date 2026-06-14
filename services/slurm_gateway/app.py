"""Standalone Slurm gateway ASGI application factory.

This builds a *bounded* FastAPI app that mounts only the Slurm router
(`/api/v1/slurm/health` + `/api/v1/slurm/*`). It deliberately includes no forecast/model/
pipeline/static/frontend business routes, so a node-22 deployment of this app
cannot expose business surfaces. The full business API (``apps.api.main``)
remains the only place those routes are served.

The dangerous `/api/v1/slurm/internal/reset` endpoint clears gateway state and is
therefore *not registered* unless ``SLURM_GATEWAY_ALLOW_INTERNAL_RESET`` is
explicitly enabled. When disabled it is absent from the route inventory (404)
rather than merely returning 403.
"""

from __future__ import annotations

from fastapi import FastAPI

from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.routes import create_slurm_router

INTERNAL_RESET_PATH = "/api/v1/slurm/internal/reset"


def create_gateway_app(settings: SlurmGatewaySettings | None = None) -> FastAPI:
    """Build the standalone, business-route-free Slurm gateway app."""

    settings = settings or get_settings()
    app = FastAPI(
        title="NHMS Slurm Gateway",
        description="Standalone Slurm submission gateway (no business routes).",
        version="0.1.0",
    )
    app.include_router(create_slurm_router(include_internal_reset=settings.allow_internal_reset))
    return app
