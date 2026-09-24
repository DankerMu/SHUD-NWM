"""Response models for ``apps/api/routes/data_sources.py`` (#2348).

Store rows go through ``_json_ready`` (``packages/common/forecast_store.py``)
and station series through ``read_station_forcing_csv``, so every timestamp is
already a string. ``provider``/``description`` are read out of the operator's
``config_json`` and keep its JSON type (``Any``).
"""

from __future__ import annotations

from typing import Any

from apps.api.response_models.envelope import OkEnvelope, OpenModel

Number = int | float


class DataSource(OpenModel):
    """``SELECT * FROM met.data_source`` through ``_data_source_response``."""

    source_id: str
    source_name: str
    source_type: str
    status: str
    native_format: str | None
    license_status: str | None
    adapter_name: str
    config_json: dict[str, Any]
    created_at: str
    provider: Any
    source: str
    format: str | None
    description: Any


class DataSourcePage(OpenModel):
    items: list[DataSource]
    total_count: int
    limit: int
    offset: int


class ForecastCycle(OpenModel):
    """``SELECT * FROM met.forecast_cycle`` through ``_cycle_response``."""

    cycle_id: str
    source_id: str
    cycle_time: str
    issue_time: str | None
    status: str
    manifest_uri: str | None
    retry_count: int
    error_code: str | None
    error_message: str | None
    created_at: str
    file_count: int | None
    quality_flag: str


class ForecastCyclePage(OpenModel):
    items: list[ForecastCycle]
    total_count: int
    limit: int
    offset: int


class MetStation(OpenModel):
    """``_station_response``: ``longitude``/``latitude``/``elevation`` are
    ``float(...)`` casts, ``elevation_m`` the raw double column."""

    station_id: str
    basin_version_id: str
    station_name: str | None = None
    name: str | None = None
    longitude: Number | None = None
    latitude: Number | None = None
    elevation_m: Number | None = None
    elevation: Number | None = None
    station_role: str
    properties_json: dict[str, Any] | None = None
    created_at: str


class MetStationPage(OpenModel):
    items: list[MetStation]
    total_count: int
    limit: int
    offset: int
    filters: dict[str, Any] | None = None


class StationSeriesPoint(OpenModel):
    valid_time: str
    value: Number
    quality_flag: str | None
    source_id: str | None = None


class StationSeriesStation(OpenModel):
    """``StationMetadata.response()``."""

    station_id: str
    basin_version_id: str
    station_name: str | None = None
    name: str | None = None
    longitude: Number | None = None
    latitude: Number | None = None
    elevation_m: Number | None = None
    elevation: Number | None = None
    station_role: str | None = None
    active_flag: bool | None = None
    properties_json: dict[str, Any] | None = None
    created_at: str | None = None


class StationSeriesMetadata(OpenModel):
    limit: int
    returned_points: int
    requested_from: str | None
    requested_to: str | None
    returned_from: str | None
    returned_to: str | None
    truncated: bool


class StationSeries(OpenModel):
    variable: str
    unit: str | None
    native_resolution: str | None
    source_id: str | None = None
    cycle_time: str | None = None
    points: list[StationSeriesPoint]
    truncated: bool
    metadata: StationSeriesMetadata


class StationSeriesResponse(OpenModel):
    """``read_station_forcing_csv`` (``packages/common/object_store_forcing.py``)."""

    station_id: str
    station: StationSeriesStation
    forcing_version_id: str
    model_id: str | None = None
    source_id: str
    cycle_time: str | None = None
    valid_time_start: str | None = None
    valid_time_end: str | None = None
    limit: int
    requested_from: str | None = None
    requested_to: str | None = None
    series: list[StationSeries]


class DataSourcePageEnvelope(OkEnvelope):
    data: DataSourcePage


class ForecastCyclePageEnvelope(OkEnvelope):
    data: ForecastCyclePage


class MetStationPageEnvelope(OkEnvelope):
    data: MetStationPage


class StationSeriesResponseEnvelope(OkEnvelope):
    data: StationSeriesResponse
