"""Response models for ``apps/api/routes/forecast.py`` (#2222, #2348).

Every datetime here is already a string when the handler returns it
(``_json_ready`` / ``_format_time`` in ``packages/common/forecast_store.py``),
so the fields are ``str``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from apps.api.response_models.envelope import OkEnvelope, OpenModel

# --------------------------------------------------------------------------- #
# Hydro runs (#2222)
# --------------------------------------------------------------------------- #


class HydroRun(BaseModel):
    """The public run projection (``HYDRO_RUN_PUBLIC_FIELDS``).

    The one filtering model of #2348: ``extra="ignore"`` drops any key outside
    the allowlist at runtime (``extra="forbid"`` would turn it into a 500).
    ``required``/nullability follow the published hand schema
    (``openapi_restored_schemas._hydro_run_schema``); the timestamps are the
    ``_json_ready`` strings.

    ``run_type`` / ``status`` are ``str``, not the published ``RunType`` /
    ``RunStatus`` enums: the value is the live ``hydro.run_type`` /
    ``hydro.run_status`` label, and the live enum is not bounded by the repo
    migrations (node-27, 2026-09-24: ``run_status`` carries ``frequency_done``,
    which no migration creates). A closed runtime enum would 500 the whole
    ``/runs`` page on such a row, where master passed the label through; the
    hand schema stays the published contract.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str
    run_key: int | None = None
    run_type: str
    scenario_id: str
    model_id: str
    basin_id: str | None = None
    basin_version_id: str
    river_network_version_id: str | None
    forcing_version_id: str | None = None
    init_state_id: str | None = None
    source_id: str | None = None
    source: str | None = None
    cycle_time: str | None = None
    status: str
    slurm_job_id: str | None = None
    start_time: str
    end_time: str
    run_manifest_uri: str | None = None
    output_uri: str | None = None
    log_uri: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    parsed_at: str | None = None
    created_at: str
    updated_at: str


class HydroRunPage(BaseModel):
    """``store.list_runs`` through ``routes.forecast._paginated_payload``."""

    model_config = ConfigDict(extra="ignore")

    items: list[HydroRun]
    total: int
    total_count: int | None = None
    limit: int
    offset: int


class HydroRunEnvelope(OkEnvelope):
    data: HydroRun


class HydroRunPageEnvelope(OkEnvelope):
    data: HydroRunPage


# --------------------------------------------------------------------------- #
# River forecast series (bare body, no envelope)
# --------------------------------------------------------------------------- #


class SeriesSegment(OpenModel):
    """One ``series[]`` entry; ``source_id``/``cycle_time``/``available_lead_hours``
    are omitted (not null) when unavailable (``_forecast_series_metadata``)."""

    scenario_id: str
    source_id: str | None = None
    cycle_time: str | None = None
    available_lead_hours: int | None = None
    segment_role: str
    variable: str | None = None
    # ``[epoch_ms:int, value:float]`` pairs: ``list[Any]`` keeps the int/float
    # split byte-identical and skips per-point validation.
    points: list[Any]


class RiverSeriesResponse(OpenModel):
    segment_id: str
    issue_time: str | None
    unit: str
    series: list[SeriesSegment]


class SplicedSeriesPoint(OpenModel):
    valid_time: str
    value: int | float


class SplicedSeriesSegment(OpenModel):
    scenario: str
    scenario_id: str | None = None
    source: str
    source_id: str | None = None
    cycle_time: str | None = None
    available_lead_hours: int | None = None
    segment_role: str
    data: list[SplicedSeriesPoint]


class SplicedForecastResponse(OpenModel):
    river_segment_id: str
    segments: list[SplicedSeriesSegment]
    issue_time: str | None
    variable: str
    unit: str


ForecastSeriesResponse = RiverSeriesResponse | SplicedForecastResponse


# --------------------------------------------------------------------------- #
# QHH latest product (full and identity_only share one shape)
# --------------------------------------------------------------------------- #


class QhhLatestUnavailableReason(OpenModel):
    code: str
    message: str
    run_id: str | None = None
    source_id: str | None = None


class QhhLatestQualityNote(OpenModel):
    code: str
    message: str
    expected_horizon_hours: int | None = None
    available_horizon_hours: int | None = None
    available_end_time: str | None = None


class QhhLatestStationVariableCoverage(OpenModel):
    variable: str
    station_count: int
    sample_count: int
    unit_count: int
    quality_flag_count: int
    missing_unit_samples: int
    missing_quality_flag_samples: int
    valid_time_start: str | None
    valid_time_end: str | None


class QhhLatestQueryIndex(OpenModel):
    table: str
    index: str
    status: str
    columns: list[str]
    predicate: str | None = None


class QhhLatestAvailability(OpenModel):
    ready: bool
    unavailable_reasons: list[QhhLatestUnavailableReason]
    quality_flags: list[str]
    quality_notes: list[QhhLatestQualityNote]


class QhhLatestQuality(OpenModel):
    station_sample_count: int
    river_sample_count: int
    required_station_variables: list[str]
    station_variable_coverage: list[QhhLatestStationVariableCoverage]
    candidate_limit: int
    search_limit: int
    context_limit: int
    query_indexes: list[QhhLatestQueryIndex]


class QhhLatestProduct(OpenModel):
    """``_qhh_latest_candidate_response`` (full) / ``_qhh_identity_product``
    (``identity_only``, which adds ``available_issue_times``)."""

    basin_id: str
    model_id: str
    basin_version_id: str
    river_network_version_id: str
    available_issue_times: list[str] | None = None
    source_id: str
    cycle_time: str
    run_id: str
    forcing_version_id: str
    station_count: int
    expected_station_count: int | None
    segment_count: int
    expected_segment_count: int | None
    status: str
    run_status: str
    valid_time_start: str | None
    valid_time_end: str | None
    river_valid_time_start: str | None
    river_valid_time_end: str | None
    forcing_valid_time_start: str | None
    forcing_valid_time_end: str | None
    available_horizon_hours: int | None
    expected_horizon_hours: int
    shorter_horizon: bool
    availability: QhhLatestAvailability
    quality: QhhLatestQuality


class QhhLatestProductEnvelope(OkEnvelope):
    data: QhhLatestProduct
