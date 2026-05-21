from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from apps.api.auth import PolicyDecision, require_action
from apps.api.errors import ApiError
from packages.common.model_registry import (
    RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
    RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    MissingResourceError,
    ModelLifecycleAuditPersistenceError,
    ModelLifecycleOperation,
    ModelRegistryError,
    PsycopgModelRegistryStore,
    RiverSegmentGeoJsonBudgetError,
    sanitize_model_detail_payload,
    sanitize_model_list_payload,
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
    active_flag: bool = Field(
        default=False,
        description="Must be false on creation; activate models through the lifecycle operation endpoint.",
    )
    resource_profile: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_package_uri")
    @classmethod
    def _model_package_uri_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_package_uri is required")
        return value

    @field_validator("active_flag")
    @classmethod
    def _active_flag_must_not_create_active(cls, value: bool) -> bool:
        if value:
            raise ValueError("active_flag=true is not accepted when creating models; use lifecycle activate")
        return False


class ActiveFlagPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = Field(
        validation_alias=AliasChoices("active", "active_flag"),
        description="Canonical active flag. The legacy active_flag key is accepted for compatibility.",
    )


class ModelLifecyclePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ModelLifecycleOperation
    previous_model_id: str | None = None
    override_missing_active: bool = False
    reason: str | None = None


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


def require_model_admin_action(
    request: Request,
    target_id: str,
    payload: dict[str, Any] | None = None,
) -> PolicyDecision:
    return require_action(
        request,
        "models.switch_version",
        target_type="model_registry",
        target_id=target_id,
        payload=payload,
    )


def require_create_basin_action(request: Request) -> PolicyDecision:
    return require_model_admin_action(request, "basins")


def require_create_basin_version_action(basin_id: str, request: Request) -> PolicyDecision:
    return require_model_admin_action(request, basin_id)


def require_create_river_network_action(request: Request) -> PolicyDecision:
    return require_model_admin_action(request, "river-networks")


def require_create_mesh_version_action(request: Request) -> PolicyDecision:
    return require_model_admin_action(request, "mesh-versions")


def require_create_model_action(request: Request) -> PolicyDecision:
    return require_model_admin_action(request, "models")


def require_create_crosswalk_action(request: Request) -> PolicyDecision:
    return require_model_admin_action(request, "river-segment-crosswalks")


def require_model_active_action(
    request: Request,
    model_id: str,
    payload: ActiveFlagPayload,
) -> PolicyDecision:
    action_id = "models.activate" if payload.active else "models.deactivate"
    return require_action(
        request,
        action_id,
        target_type="model_instance",
        target_id=model_id,
        payload={"model_id": model_id, "active": payload.active},
    )


def require_model_lifecycle_action(
    request: Request,
    model_id: str,
    payload: ModelLifecyclePayload,
) -> PolicyDecision:
    action_id = {
        "activate": "models.activate",
        "deactivate": "models.deactivate",
        "switch_version": "models.switch_version",
        "rollback_version": "models.rollback_version",
        "supersede": "models.supersede",
        "deprecate": "models.deactivate",
    }[payload.operation]
    return require_action(
        request,
        action_id,
        target_type="model_instance",
        target_id=model_id,
        payload=payload.model_dump(),
    )


def _handle_registry_error(error: Exception) -> ApiError:
    if isinstance(error, ModelLifecycleAuditPersistenceError):
        return ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="MODEL_LIFECYCLE_AUDIT_PERSISTENCE_FAILED",
            message="Model lifecycle audit evidence could not be persisted.",
            details=error.result,
        )
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
    if isinstance(error, RiverSegmentGeoJsonBudgetError):
        return ApiError(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED",
            message=(
                "River segment GeoJSON payload budget exceeded; request fewer segments "
                "or a more specific river network."
            ),
            details={
                "error_type": error.__class__.__name__,
                "limit_type": error.limit_type,
                "max_bytes": error.max_bytes,
                "serialized_bytes": error.serialized_bytes,
                "scope": error.scope,
            },
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
    request: Request,
    payload: BasinCreatePayload,
    policy_decision: PolicyDecision = Depends(require_create_basin_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.create_basin_with_version(payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/basins/{basin_id}/versions", status_code=status.HTTP_201_CREATED)
def create_basin_version(
    request: Request,
    basin_id: str,
    payload: BasinVersionPayload,
    policy_decision: PolicyDecision = Depends(require_create_basin_version_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.create_basin_version(basin_id, payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/river-networks", status_code=status.HTTP_201_CREATED)
def create_river_network(
    request: Request,
    payload: RiverNetworkCreatePayload,
    policy_decision: PolicyDecision = Depends(require_create_river_network_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.create_river_network(payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get(
    "/basin-versions/{basin_version_id}/river-segments",
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "River segment GeoJSON payload budget exceeded.",
        },
    },
)
def list_river_segments(
    request: Request,
    basin_version_id: str,
    river_network_version_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        data = store.list_river_segments(
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            limit=limit,
            offset=offset,
        )
        _enforce_river_segment_response_budget(
            data,
            max_bytes=RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
            scope="collection",
        )
        return _ok(
            request,
            data,
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.get(
    "/basin-versions/{basin_version_id}/river-segments/{segment_id}",
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "River segment GeoJSON payload budget exceeded.",
        },
    },
)
def get_river_segment(
    request: Request,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str = Query(..., min_length=1),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        data = store.get_river_segment(
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
            segment_id=segment_id,
        )
        _enforce_river_segment_response_budget(
            data,
            max_bytes=RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
            scope="detail",
        )
        return _ok(
            request,
            data,
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


def _enforce_river_segment_response_budget(payload: dict[str, Any], *, max_bytes: int, scope: str) -> None:
    serialized_bytes = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    if serialized_bytes > max_bytes:
        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=max_bytes,
            serialized_bytes=serialized_bytes,
            scope=scope,
        )


@router.post("/mesh-versions", status_code=status.HTTP_201_CREATED)
def create_mesh_version(
    request: Request,
    payload: MeshVersionCreatePayload,
    policy_decision: PolicyDecision = Depends(require_create_mesh_version_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.create_mesh_version(payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(
    request: Request,
    payload: ModelCreatePayload,
    policy_decision: PolicyDecision = Depends(require_create_model_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        validate_model_package_uri(payload.model_package_uri)
        return _ok(request, store.create_model(payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.put("/models/{model_id}/active")
def set_model_active(
    request: Request,
    model_id: str,
    payload: ActiveFlagPayload,
    policy_decision: PolicyDecision = Depends(require_model_active_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        result = store.model_lifecycle_operation(
            model_id,
            operation="activate" if payload.active else "deactivate",
            policy_decision=policy_decision,
            request_id=getattr(request.state, "request_id", None),
        )
        return _ok(request, result)
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/models/{model_id}/preflight")
def preflight_model_lifecycle(
    request: Request,
    model_id: str,
    payload: ModelLifecyclePayload,
    policy_decision: PolicyDecision = Depends(require_model_lifecycle_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.preflight_model_operation(
                model_id,
                operation=payload.operation,
                policy_decision=policy_decision,
                previous_model_id=payload.previous_model_id,
                override_missing_active=payload.override_missing_active,
                reason=payload.reason,
                request_id=getattr(request.state, "request_id", None),
            ),
        )
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/models/{model_id}/lifecycle")
def model_lifecycle_operation(
    request: Request,
    model_id: str,
    payload: ModelLifecyclePayload,
    policy_decision: PolicyDecision = Depends(require_model_lifecycle_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(
            request,
            store.model_lifecycle_operation(
                model_id,
                operation=payload.operation,
                policy_decision=policy_decision,
                request_id=getattr(request.state, "request_id", None),
                previous_model_id=payload.previous_model_id,
                override_missing_active=payload.override_missing_active,
                reason=payload.reason,
            ),
        )
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
            sanitize_model_list_payload(store.list_models(
                basin_version_id=basin_version_id,
                active=active_filter,
                limit=limit,
                offset=offset,
            )),
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
        return _ok(request, sanitize_model_detail_payload(store.get_model(model_id)))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


@router.post("/river-segment-crosswalks", status_code=status.HTTP_201_CREATED)
def create_river_segment_crosswalks(
    request: Request,
    payload: CrosswalkCreatePayload,
    policy_decision: PolicyDecision = Depends(require_create_crosswalk_action),
    store: PsycopgModelRegistryStore = Depends(get_model_registry_store),
) -> dict[str, Any]:
    try:
        return _ok(request, store.create_crosswalk_entries(payload.model_dump(), policy_decision=policy_decision))
    except (ModelRegistryError, ModelPackageValidationError) as error:
        raise _handle_registry_error(error) from error
    except Exception as error:
        raise _handle_registry_error(error) from error


def _ok(request: Request, data: Any) -> dict[str, Any]:
    body = {
        "request_id": getattr(request.state, "request_id", None) or str(uuid4()),
        "status": "ok",
        "data": data,
    }
    decisions = getattr(request.state, "auth_policy_decisions", None)
    if decisions:
        body["auth_policy_decisions"] = decisions
    return body
