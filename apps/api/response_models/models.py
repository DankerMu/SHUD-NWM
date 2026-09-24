"""Response models for ``apps/api/routes/models.py`` (#2348).

Registry rows reach the handler as ``dict(row)`` with raw driver values, so
timestamps are ``datetime`` objects (serialized ``...Z``) and JSONB columns are
mappings. Lineage keys that ``_model_asset_detail`` copies out of
``resource_profile`` keep whatever JSON type the profile holds, so they are
typed ``Any``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from apps.api.response_models.envelope import OkEnvelope, OpenModel

Number = int | float


class GeoJsonGeometry(OpenModel):
    """A PostGIS ``ST_AsGeoJSON(...)::json`` object.

    ``type`` stays a plain string (``ST_LineSubstring`` can degrade to a
    ``Point``) and ``coordinates`` a ``list[Any]``: nested positions are neither
    re-validated nor re-typed. ``crs`` and any other member pass through.
    """

    type: str
    coordinates: list[Any]


# --------------------------------------------------------------------------- #
# Basins
# --------------------------------------------------------------------------- #


class Basin(OpenModel):
    basin_id: str
    basin_name: str
    basin_group: str | None = None
    description: str | None = None
    created_at: datetime


class BasinVersion(OpenModel):
    basin_version_id: str
    basin_id: str
    version_label: str
    geom: GeoJsonGeometry
    active_flag: bool
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_uri: str | None = None
    checksum: str | None = None
    created_at: datetime


class BasinRecord(OpenModel):
    """``INSERT INTO core.basin ... RETURNING *``."""

    basin_id: str
    basin_name: str
    basin_group: str | None
    description: str | None
    created_at: datetime


class BasinVersionRecord(OpenModel):
    """``_insert_basin_version``'s ``RETURNING`` row (unredacted write echo)."""

    basin_version_id: str
    basin_id: str
    version_label: str
    geom: GeoJsonGeometry
    active_flag: bool
    valid_from: datetime | None
    valid_to: datetime | None
    source_uri: str | None
    checksum: str | None
    created_at: datetime


class BasinCreateResult(OpenModel):
    basin: BasinRecord
    basin_version: BasinVersionRecord


# --------------------------------------------------------------------------- #
# River networks, segments, meshes, crosswalks
# --------------------------------------------------------------------------- #


class RiverNetworkVersionRecord(OpenModel):
    """``INSERT INTO core.river_network_version ... RETURNING *``."""

    river_network_version_id: str
    basin_version_id: str
    version_label: str
    segment_count: int
    source_uri: str | None
    checksum: str | None
    created_at: datetime


class RiverNetworkCreateResult(OpenModel):
    river_network_version: RiverNetworkVersionRecord
    segment_count: int


class RiverSegmentProperties(OpenModel):
    """Feature ``properties``: the reach branch spreads ``properties_json``
    first, the Path C slice branch adds ``iRiv``/``iEle``/``reach_segment_id``;
    both pass through as extras."""

    segment_id: str
    river_segment_id: str
    basin_version_id: str
    river_network_version_id: str
    name: str
    stream_order: int
    segment_order: int | None = None
    downstream_segment_id: str | None = None
    length_m: Number | None = None


class RiverSegmentFeature(OpenModel):
    type: str
    id: str | None = None
    properties: RiverSegmentProperties
    geometry: GeoJsonGeometry


class RiverSegmentFeatureCollection(OpenModel):
    type: str
    features: list[RiverSegmentFeature]
    total: int
    feature_total: int
    limit: int
    offset: int


class RiverSegment(OpenModel):
    """``_river_segment_detail``."""

    river_segment_id: str
    river_network_version_id: str
    segment_order: int | None = None
    downstream_segment_id: str | None = None
    length_m: Number | None = None
    geom: GeoJsonGeometry
    properties_json: dict[str, Any]
    created_at: datetime


class MeshVersionRecord(OpenModel):
    """``INSERT INTO core.mesh_version ... RETURNING *``."""

    mesh_version_id: str
    basin_version_id: str
    version_label: str
    mesh_uri: str
    checksum: str | None
    properties_json: dict[str, Any]
    created_at: datetime


class CrosswalkRecord(OpenModel):
    river_network_version_id: str
    river_segment_id: str
    source: str
    external_id: str
    properties_json: dict[str, Any]


class CrosswalkCreateResult(OpenModel):
    count: int
    items: list[CrosswalkRecord]


# --------------------------------------------------------------------------- #
# Model instances and lifecycle
# --------------------------------------------------------------------------- #


class ModelInstance(OpenModel):
    """The union of the list projection, the detail projection and the
    lifecycle rows (see ``openapi_restored_schemas._model_instance_schema``)."""

    model_id: str
    model_name: str | None = None
    basin_id: str | None = None
    basin_name: str | None = None
    basin_version_id: str
    basin_checksum: str | None = None
    river_network_version_id: str
    river_network_checksum: str | None = None
    mesh_version_id: str
    calibration_version_id: str
    segment_count: int | None = None
    mesh_uri: str | None = None
    mesh_checksum: str | None = None
    shud_code_version: str
    rshud_code_version: str | None = None
    autoshud_code_version: str | None = None
    active_flag: bool
    lifecycle_state: str
    container_image: str | None = None
    model_package_uri: str | None
    # MODEL_ASSET_LINEAGE_KEYS: copied out of resource_profile / mesh properties.
    package_checksum: Any = None
    manifest_uri: Any = None
    source_inventory_checksum: Any = None
    basin_slug: Any = None
    shud_input_name: Any = None
    source_path: Any = None
    resolved_source_path: Any = None
    source_uri: Any = None
    source_is_symlink: Any = None
    resource_profile: dict[str, Any]
    created_at: datetime


class ModelInstanceRecord(OpenModel):
    """``INSERT INTO core.model_instance ... RETURNING *`` (write echo)."""

    model_id: str
    basin_version_id: str
    river_network_version_id: str
    mesh_version_id: str
    calibration_version_id: str
    shud_code_version: str
    rshud_code_version: str | None
    autoshud_code_version: str | None
    container_image: str | None
    model_package_uri: str
    active_flag: bool
    lifecycle_state: str
    resource_profile: dict[str, Any]
    created_at: datetime


class ModelInstancePage(OpenModel):
    items: list[ModelInstance]
    total: int
    limit: int
    offset: int


class ModelOperationPreflight(OpenModel):
    """``_build_model_operation_preflight`` (optionally rewritten by
    ``_apply_idempotent_rollback_preflight``)."""

    # ``schema`` would shadow ``BaseModel.schema``; the wire key is the alias.
    schema_: str = Field(alias="schema")
    request_id: str | None = None
    operation: str
    action_id: str | None = None
    actor_id: str | None = None
    roles: list[str] | None = None
    status: str
    model_id: str
    basin_id: str | None = None
    basin_version_id: str | None = None
    current_active_model_id: str | None = None
    previous_model_id: str | None = None
    restored_model_id: str | None = None
    prior_audit_log_id: int | None = None
    rollback_history: dict[str, Any] | None = None
    river_network_version_id: str | None = None
    mesh_version_id: str | None = None
    lineage: dict[str, Any] | None = None
    object_uri_prefix: dict[str, Any] | None = None
    impact: dict[str, Any]
    blockers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    override_missing_active: bool | None = None
    reason: str | None = None


class ModelLifecycleResult(OpenModel):
    status: str
    operation: str
    model: ModelInstance
    previous_model: ModelInstance | None = None
    preflight: ModelOperationPreflight
    audit_reference: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Envelopes
# --------------------------------------------------------------------------- #


class BasinListEnvelope(OkEnvelope):
    data: list[Basin]


class BasinVersionListEnvelope(OkEnvelope):
    data: list[BasinVersion]


class BasinCreateResultEnvelope(OkEnvelope):
    data: BasinCreateResult


class BasinVersionRecordEnvelope(OkEnvelope):
    data: BasinVersionRecord


class RiverNetworkCreateResultEnvelope(OkEnvelope):
    data: RiverNetworkCreateResult


class RiverSegmentFeatureCollectionEnvelope(OkEnvelope):
    data: RiverSegmentFeatureCollection


class RiverSegmentEnvelope(OkEnvelope):
    data: RiverSegment


class MeshVersionRecordEnvelope(OkEnvelope):
    data: MeshVersionRecord


class ModelInstanceRecordEnvelope(OkEnvelope):
    data: ModelInstanceRecord


class ModelInstancePageEnvelope(OkEnvelope):
    data: ModelInstancePage


class ModelInstanceEnvelope(OkEnvelope):
    data: ModelInstance


class ModelOperationPreflightEnvelope(OkEnvelope):
    data: ModelOperationPreflight


class ModelLifecycleResultEnvelope(OkEnvelope):
    data: ModelLifecycleResult


class CrosswalkCreateResultEnvelope(OkEnvelope):
    data: CrosswalkCreateResult
