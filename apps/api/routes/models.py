from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

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
    return PsycopgModelRegistryStore.from_env()


def _handle_registry_error(error: Exception) -> HTTPException:
    if isinstance(error, DuplicateResourceError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, MissingResourceError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, InvalidReferenceError | InvalidPayloadError | ModelPackageValidationError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, ModelRegistryError):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unexpected model registry error.")


@router.post("/basins", status_code=status.HTTP_201_CREATED)
def create_basin(
    payload: BasinCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_basin_with_version(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
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


@router.post("/river-networks", status_code=status.HTTP_201_CREATED)
def create_river_network(
    payload: RiverNetworkCreatePayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.create_river_network(payload.model_dump())
    except (ModelRegistryError, ModelPackageValidationError) as error:
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


@router.put("/models/{model_id}/active")
def set_model_active(
    model_id: str,
    payload: ActiveFlagPayload,
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return store.set_model_active(model_id, payload.active)
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error


@router.get("/models")
def list_models(
    basin_version_id: str | None = None,
    active: Literal["true", "false", "all"] = Query(default="true"),
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
        return store.list_models(
            basin_version_id=basin_version_id,
            active=active_filter,
            limit=limit,
            offset=offset,
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
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
