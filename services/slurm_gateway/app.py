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

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from packages.common.openapi_auth_security import security_scheme_definitions
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.routes import create_slurm_router

INTERNAL_RESET_PATH = "/api/v1/slurm/internal/reset"


def _standalone_openapi_factory(app: FastAPI) -> Callable[[], dict[str, Any]]:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        _publish_standalone_security_schemes(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    return custom_openapi


def _publish_standalone_security_schemes(schema: dict) -> None:
    """Publish every referenced security scheme so none dangles.

    FastAPI emits only the route-level ``HTTPBearer`` ``SlurmServiceBearer``
    scheme; the operation-level ``openapi_extra`` alternatives reference the
    five identity schemes, so this step overwrites the full six-scheme set
    from the shared owner. Exact assignment (not ``setdefault``) keeps a stale
    same-name scheme from surviving. No business routes or credentials are
    added.
    """
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes.update(security_scheme_definitions())


def create_gateway_app(settings: SlurmGatewaySettings | None = None) -> FastAPI:
    """Build the standalone, business-route-free Slurm gateway app."""

    settings = settings or get_settings()
    app = FastAPI(
        title="NHMS Slurm Gateway",
        description="Standalone Slurm submission gateway (no business routes).",
        version="0.1.0",
    )
    app.include_router(create_slurm_router(include_internal_reset=settings.allow_internal_reset))
    app.openapi = _standalone_openapi_factory(app)
    return app
