from __future__ import annotations

import pytest

from workers.data_adapters.base import (
    generate_segmented_forecast_hours,
    parse_resolution_segments,
    validate_forecast_hours,
)


def test_parse_resolution_segments_parses_ascending_segments() -> None:
    assert parse_resolution_segments("120:1,384:3") == ((120, 1), (384, 3))


def test_parse_resolution_segments_accepts_semicolon_separator() -> None:
    # Slurm env-export value filtering rejects commas; semicolons survive it.
    assert parse_resolution_segments("120:1;384:3") == ((120, 1), (384, 3))


def test_parse_resolution_segments_returns_none_for_empty() -> None:
    assert parse_resolution_segments(None) is None
    assert parse_resolution_segments("   ") is None


@pytest.mark.parametrize("spec", ["120:0,384:3", "abc", "384:3,120:1", "120:1,120:3"])
def test_parse_resolution_segments_rejects_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_resolution_segments(spec)


def test_generate_segmented_forecast_hours_gfs_native_grid() -> None:
    # GFS: hourly to 120h, 3-hourly beyond; the boundary realigns to the 3h grid (123).
    hours = generate_segmented_forecast_hours(0, 168, ((120, 1), (384, 3)))
    assert hours[:5] == [0, 1, 2, 3, 4]
    assert 120 in hours and 121 not in hours and 122 not in hours
    assert hours[hours.index(120) + 1] == 123
    assert hours[-1] == 168
    assert hours == sorted(set(hours))


def test_generate_segmented_forecast_hours_ifs_native_grid() -> None:
    # IFS: 3-hourly to 144h, 6-hourly beyond.
    hours = generate_segmented_forecast_hours(0, 168, ((144, 3), (360, 6)))
    assert hours[:3] == [0, 3, 6]
    assert 144 in hours
    assert hours[hours.index(144) + 1] == 150
    assert hours[-1] == 168


def test_generate_segmented_forecast_hours_short_horizon() -> None:
    assert generate_segmented_forecast_hours(0, 12, ((6, 1), (168, 3))) == [0, 1, 2, 3, 4, 5, 6, 9, 12]


def test_validate_forecast_hours_membership_accepts_native_schedule() -> None:
    allowed = set(generate_segmented_forecast_hours(0, 168, ((120, 1), (384, 3))))
    assert validate_forecast_hours(
        [0, 1, 120, 123],
        source_id="GFS",
        min_hour=0,
        max_hour=168,
        step_hours=1,
        allowed_hours=allowed,
    ) == [0, 1, 120, 123]


def test_validate_forecast_hours_membership_rejects_off_schedule_hour() -> None:
    allowed = set(generate_segmented_forecast_hours(0, 168, ((120, 1), (384, 3))))
    with pytest.raises(ValueError, match="native resolution schedule"):
        validate_forecast_hours(
            [121],
            source_id="GFS",
            min_hour=0,
            max_hour=168,
            step_hours=1,
            allowed_hours=allowed,
        )


def test_validate_forecast_hours_uniform_still_enforced_without_allowed() -> None:
    with pytest.raises(ValueError, match="aligned to 3h"):
        validate_forecast_hours([4], source_id="GFS", min_hour=0, max_hour=168, step_hours=3)
