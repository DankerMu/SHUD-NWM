from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.best_available import get_best_available_manager
from packages.common.best_available import (
    BestAvailableManager,
    BestAvailableSelection,
    ForcingInputSelection,
    fallback_order_for_valid_time,
    source_priority,
)


class FakeBestAvailableRepository:
    def __init__(self, *, enabled_sources: tuple[str, ...] = ("ERA5", "GFS")) -> None:
        self.enabled_sources = enabled_sources
        self.forcing_inputs: list[ForcingInputSelection] = []
        self.selections: dict[tuple[str, datetime, str], BestAvailableSelection] = {}
        self.upsert_count = 0

    def list_enabled_sources(self) -> tuple[str, ...]:
        return self.enabled_sources

    def list_forcing_inputs(self, _forcing_version_id: str) -> list[ForcingInputSelection]:
        return self.forcing_inputs

    def upsert_selection(self, selection: BestAvailableSelection) -> dict[str, Any]:
        self.upsert_count += 1
        key = (selection.forcing_version_id, _dt(selection.valid_time), selection.variable)
        existing = self.selections.get(key)
        if existing is None or source_priority(selection.selected_source) >= source_priority(existing.selected_source):
            self.selections[key] = selection
        return _selection_response(self.selections[key])

    def list_selections(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        variable: str | None,
    ) -> list[dict[str, Any]]:
        rows = [
            selection
            for selection in self.selections.values()
            if _dt(from_time) <= _dt(selection.valid_time) <= _dt(to_time)
            and (variable is None or selection.variable == variable)
        ]
        rows.sort(key=lambda selection: (selection.valid_time, selection.forcing_version_id, selection.variable))
        return [_selection_response(selection) for selection in rows]


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


def test_best_available_upsert_is_idempotent_for_same_source() -> None:
    repository = FakeBestAvailableRepository()
    repository.forcing_inputs = [
        ForcingInputSelection(
            valid_time=_dt("2026-05-04T00:00:00Z"),
            variable="prcp_rate_or_amount",
            selected_source="GFS",
            source_cycle_time=_dt("2026-05-01T00:00:00Z"),
        )
    ]
    manager = BestAvailableManager(repository)

    first = manager.write_forcing_version("forcing_001", now=_dt("2026-05-08T00:00:00Z"))
    second = manager.write_forcing_version("forcing_001", now=_dt("2026-05-08T00:00:00Z"))

    assert first == second
    assert len(repository.selections) == 1
    selection = next(iter(repository.selections.values()))
    assert selection.selected_source == "GFS"
    assert selection.quality_flag == "best_available_degraded"
    assert selection.fallback_order == ("ERA5", "GFS")


def test_era5_overwrites_existing_gfs_selection() -> None:
    repository = FakeBestAvailableRepository()
    manager = BestAvailableManager(repository)
    valid_time = _dt("2026-04-20T00:00:00Z")
    repository.forcing_inputs = [
        ForcingInputSelection(valid_time, "air_temperature_2m", "GFS", _dt("2026-04-19T00:00:00Z"))
    ]
    manager.write_forcing_version("forcing_gfs", now=_dt("2026-05-08T00:00:00Z"))
    repository.forcing_inputs = [
        ForcingInputSelection(valid_time, "air_temperature_2m", "ERA5", _dt("2026-04-20T00:00:00Z"))
    ]

    manager.write_forcing_version("forcing_era5", now=_dt("2026-05-08T00:00:00Z"))

    selection = repository.selections[("forcing_era5", valid_time, "air_temperature_2m")]
    assert selection.selected_source == "ERA5"
    assert selection.quality_flag == "best_available_realtime"
    assert selection.fallback_order == ("ERA5",)


def test_best_available_preserves_different_forcing_versions_with_same_time_and_variable() -> None:
    repository = FakeBestAvailableRepository()
    manager = BestAvailableManager(repository)
    valid_time = _dt("2026-05-04T00:00:00Z")
    repository.forcing_inputs = [
        ForcingInputSelection(valid_time, "prcp_rate_or_amount", "GFS", _dt("2026-05-01T00:00:00Z"))
    ]
    manager.write_forcing_version("forcing_model_a", now=_dt("2026-05-08T00:00:00Z"))
    repository.forcing_inputs = [
        ForcingInputSelection(valid_time, "prcp_rate_or_amount", "GFS", _dt("2026-05-02T00:00:00Z"))
    ]

    manager.write_forcing_version("forcing_model_b", now=_dt("2026-05-08T00:00:00Z"))

    assert set(repository.selections) == {
        ("forcing_model_a", valid_time, "prcp_rate_or_amount"),
        ("forcing_model_b", valid_time, "prcp_rate_or_amount"),
    }
    rows = repository.list_selections(from_time=valid_time, to_time=valid_time, variable="prcp_rate_or_amount")
    assert [row["forcing_version_id"] for row in rows] == ["forcing_model_a", "forcing_model_b"]


def test_fallback_order_filters_to_enabled_sources_by_time_window() -> None:
    now = _dt("2026-05-08T00:00:00Z")

    assert fallback_order_for_valid_time(
        _dt("2026-05-04T00:00:00Z"),
        now=now,
        enabled_sources=("ERA5", "GFS"),
    ) == ["ERA5", "GFS"]
    assert fallback_order_for_valid_time(
        _dt("2026-05-04T00:00:00Z"),
        now=now,
        enabled_sources=("CLDAS", "ERA5", "GFS"),
    ) == ["CLDAS", "ERA5", "GFS"]
    assert fallback_order_for_valid_time(
        _dt("2026-04-20T00:00:00Z"),
        now=now,
        enabled_sources=("ERA5", "GFS"),
    ) == ["ERA5"]


@pytest.mark.asyncio
async def test_best_available_api_query_returns_filtered_rows() -> None:
    repository = FakeBestAvailableRepository()
    valid_time = _dt("2026-04-20T00:00:00Z")
    repository.selections[("forcing_era5", valid_time, "prcp_rate_or_amount")] = BestAvailableSelection(
        forcing_version_id="forcing_era5",
        valid_time=valid_time,
        variable="prcp_rate_or_amount",
        selected_source="ERA5",
        source_cycle_time=valid_time,
        fallback_order=("ERA5",),
        quality_flag="best_available_realtime",
    )
    app.dependency_overrides[get_best_available_manager] = lambda: BestAvailableManager(repository)

    response = await _get("/api/v1/met/best-available?from=2026-04-20&to=2026-04-20&variable=prcp_rate_or_amount")

    assert response.status_code == 200
    assert response.json() == [
        {
            "forcing_version_id": "forcing_era5",
            "valid_time": "2026-04-20T00:00:00Z",
            "variable": "prcp_rate_or_amount",
            "selected_source": "ERA5",
            "source_cycle_time": "2026-04-20T00:00:00Z",
            "fallback_order": ["ERA5"],
            "quality_flag": "best_available_realtime",
        }
    ]


async def _get(path: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _selection_response(selection: BestAvailableSelection) -> dict[str, Any]:
    return {
        "forcing_version_id": selection.forcing_version_id,
        "valid_time": _format_time(selection.valid_time),
        "variable": selection.variable,
        "selected_source": selection.selected_source,
        "source_cycle_time": _format_time(selection.source_cycle_time),
        "fallback_order": list(selection.fallback_order),
        "quality_flag": selection.quality_flag,
    }


def _dt(value: str | datetime) -> datetime:
    candidate = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _dt(value).isoformat().replace("+00:00", "Z")
