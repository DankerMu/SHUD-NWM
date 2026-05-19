from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from apps.api.errors import ApiError
from packages.common.model_registry import (
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    MissingResourceError,
    ModelRegistryError,
    PsycopgModelRegistryStore,
)
from workers.model_registry.validator import ModelPackageValidationError, validate_model_package_uri

router = APIRouter(prefix="/api/v1", tags=["models"])
logger = logging.getLogger(__name__)
SAFE_MODEL_REGISTRY_ERROR_MESSAGE = "Model registry operation failed."


Geometry = dict[str, Any] | str


class BasinVersionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basin_version_id: str | None = None
    version_label: str
    geom: Geometry
    active_flag: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_uri: str | None = None
    checksum: str | None = None


class BasinCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basin_id: str
    basin_name: str
    basin_group: str | None = None
    description: str | None = None
    basin_version: BasinVersionPayload


class RiverSegmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    river_segment_id: str
    segment_order: int | None = None
    downstream_segment_id: str | None = None
    length_m: float | None = None
    geom: Geometry
    properties_json: dict[str, Any] = Field(default_factory=dict)


class RiverNetworkCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    river_network_version_id: str | None = None
    basin_version_id: str
    version_label: str
    segment_count: int | None = None
    source_uri: str | None = None
    checksum: str | None = None
    segments: list[RiverSegmentPayload]


class MeshVersionCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mesh_version_id: str | None = None
    basin_version_id: str
    version_label: str
    mesh_uri: str
    checksum: str | None = None
    properties_json: dict[str, Any] = Field(default_factory=dict)


class ModelCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    basin_version_id: str
    river_network_version_id: str
    mesh_version_id: str
    calibration_version_id: str
    shud_code_version: str
    rshud_code_version: str | None = None
    autoshud_code_version: str | None = None
    container_image: str | None = None
    model_package_uri: str
    active_flag: bool = False
    resource_profile: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_package_uri")
    @classmethod
    def _model_package_uri_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_package_uri is required")
        return value


class ActiveFlagPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = Field(
        validation_alias=AliasChoices("active", "active_flag"),
        description="Canonical active flag. The legacy active_flag key is accepted for compatibility.",
    )


class CrosswalkEntryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    river_segment_id: str
    source: str
    external_id: str
    properties_json: dict[str, Any] = Field(default_factory=dict)


class CrosswalkCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    river_network_version_id: str
    entries: list[CrosswalkEntryPayload]


def get_model_registry_store() -> PsycopgModelRegistryStore:
    try:
        return PsycopgModelRegistryStore.from_env()
    except ModelRegistryError as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


def _handle_registry_error(error: Exception) -> ApiError:
    if isinstance(error, DuplicateResourceError):
        return ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code="MODEL_REGISTRY_DUPLICATE",
            message=str(error),
            details={"error_type": error.__class__.__name__},
        )
    if isinstance(error, MissingResourceError):
        return ApiError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="MODEL_REGISTRY_NOT_FOUND",
            message=str(error),
            details={"error_type": error.__class__.__name__},
        )
    if isinstance(error, InvalidReferenceError):
        return ApiError(
            status_code=422,
            code="MODEL_REGISTRY_INVALID_REFERENCE",
            message=str(error),
            details={"error_type": error.__class__.__name__},
        )
    if isinstance(error, InvalidPayloadError):
        return ApiError(
            status_code=422,
            code="MODEL_REGISTRY_INVALID_PAYLOAD",
            message=str(error),
            details={"error_type": error.__class__.__name__},
        )
    if isinstance(error, ModelPackageValidationError):
        return ApiError(
            status_code=422,
            code="MODEL_PACKAGE_VALIDATION_ERROR",
            message=str(error),
            details={"error_type": error.__class__.__name__},
        )
    if isinstance(error, ModelRegistryError):
        logger.error(
            "Model registry operation failed.",
            extra={"error_type": error.__class__.__name__},
        )
        return ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="MODEL_REGISTRY_ERROR",
            message=SAFE_MODEL_REGISTRY_ERROR_MESSAGE,
            details={"error_type": error.__class__.__name__},
        )
    logger.error(
        "Unexpected model registry error.",
        extra={"error_type": error.__class__.__name__},
    )
    return ApiError(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="MODEL_REGISTRY_ERROR",
        message=SAFE_MODEL_REGISTRY_ERROR_MESSAGE,
        details={"error_type": error.__class__.__name__},
    )


@router.post("/basins", status_code=status.HTTP_201_CREATED)
def create_basin(
    payload: BasinCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_basin_with_version(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/basins/{basin_id}/versions", status_code=status.HTTP_201_CREATED)
def create_basin_version(
    basin_id: str,
    payload: BasinVersionPayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_basin_version(basin_id, payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/river-networks", status_code=status.HTTP_201_CREATED)
def create_river_network(
    payload: RiverNetworkCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_river_network(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get("/basin-versions/{basin_version_id}/river-segments")
def list_river_segments(
    request: Request,
    basin_version_id: str,
    river_network_version_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.list_river_segments(
                basin_version_id=basin_version_id,
                river_network_version_id=river_network_version_id,
                limit=limit,
                offset=offset,
            ),
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get("/basin-versions/{basin_version_id}/river-segments/{segment_id}")
def get_river_segment(
    request: Request,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str = Query(..., min_length=1),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.get_river_segment(
                basin_version_id=basin_version_id,
                river_network_version_id=river_network_version_id,
                segment_id=segment_id,
            ),
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/mesh-versions", status_code=status.HTTP_201_CREATED)
def create_mesh_version(
    payload: MeshVersionCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_mesh_version(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(
    payload: ModelCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        validate_model_package_uri(payload.model_package_uri)
        return store.create_model(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.put("/models/{model_id}/active")
def set_model_active(
    request: Request,
    model_id: str,
    payload: ActiveFlagPayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.set_model_active(model_id, payload.active))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get("/models")
def list_models(
    request: Request,
    basin_version_id: str | None = None,
    active: Literal["true", "false", "all"] = Query(
        default="true",
        description=(
            "Filter by active model flag. Omitted defaults to active models only; use all for no active filter."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    active_filter: bool | None
    if active == "all":
        active_filter = None
    else:
        active_filter = active == "true"
    try:
        return _ok(
            request,
            store.list_models(
                basin_version_id=basin_version_id,
                active=active_filter,
                limit=limit,
                offset=offset,
            ),
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get("/models/{model_id}")
def get_model(
    request: Request,
    model_id: str,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.get_model(model_id))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/river-segment-crosswalks", status_code=status.HTTP_201_CREATED)
def create_river_segment_crosswalks(
    payload: CrosswalkCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_crosswalk_entries(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


def _ok(request: Request, data: Any) -> dict[str, Any]:
    return {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }
