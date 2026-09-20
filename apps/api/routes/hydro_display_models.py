"""Response models for the hydro display routes.

Split out of `apps/api/routes/hydro_display.py` (#2026). The class `__name__`s
are unchanged, so the OpenAPI component schema names are unchanged.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Layer(BaseModel):
    layer_id: str
    layer_name: str
    layer_type: str
    variables: list[str]
    metadata: dict[str, Any] | None = None


class ApiSuccessEnvelope(BaseModel):
    request_id: str
    status: str


class LayerListResponse(ApiSuccessEnvelope):
    data: list[Layer]


class LayerValidTimes(BaseModel):
    valid_times: list[str]
    items: list[str]
    limit: int
    observed_count: int
    truncated: bool


class LayerValidTimesResponse(ApiSuccessEnvelope):
    data: LayerValidTimes


class DischargeCycle(BaseModel):
    cycle_time: str
    valid_time_start: str
    valid_time_end: str


class DischargeCycles(BaseModel):
    source: str
    cycles: list[DischargeCycle]
    default_cycle: str | None = None


class DischargeCyclesResponse(ApiSuccessEnvelope):
    data: DischargeCycles
