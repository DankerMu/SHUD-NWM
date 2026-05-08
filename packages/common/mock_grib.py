from __future__ import annotations

import json
from datetime import datetime
from typing import Any

ERA5_VARIABLES: tuple[str, ...] = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_pressure",
    "total_precipitation",
    "surface_net_solar_radiation",
    "surface_net_thermal_radiation",
)


class MockGribError(ValueError):
    """Raised when synthetic GRIB2 test payloads cannot be decoded."""


def encode_mock_grib2(payload: dict[str, Any]) -> bytes:
    """Encode a deterministic GRIB2-framed JSON payload for tests.

    The bytes include the GRIB2 indicator and end sections so file signatures are
    recognizable as GRIB edition 2. They are intentionally lightweight and are
    not a substitute for operational NOAA GRIB2 messages.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    total_length = 16 + len(body) + 4
    indicator = b"GRIB" + b"\x00\x00" + b"\x00" + b"\x02" + total_length.to_bytes(8, "big")
    return indicator + body + b"7777"


def decode_mock_grib2(content: bytes) -> dict[str, Any]:
    if len(content) < 20 or not content.startswith(b"GRIB") or not content.endswith(b"7777"):
        raise MockGribError("Payload is not a mock GRIB2 message.")
    if content[7] != 2:
        raise MockGribError("Mock GRIB payload is not GRIB edition 2.")
    expected_length = int.from_bytes(content[8:16], "big")
    if expected_length != len(content):
        raise MockGribError(f"Mock GRIB length mismatch: expected {expected_length}, actual {len(content)}.")

    try:
        payload = json.loads(content[16:-4].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MockGribError(f"Mock GRIB JSON payload is invalid: {error}") from error

    if not isinstance(payload, dict):
        raise MockGribError("Mock GRIB JSON payload must be an object.")
    return payload


def default_mock_value(variable: str, forecast_hour: int) -> float:
    if variable == "tmp2m":
        return 273.15 + 12.0 + forecast_hour * 0.05
    if variable == "apcp":
        return max(0.0, forecast_hour / 3.0)
    if variable == "rh2m":
        return min(100.0, 50.0 + forecast_hour * 0.1)
    if variable == "u10m":
        return 3.0
    if variable == "v10m":
        return 4.0
    if variable == "pressfc":
        return 101325.0
    if variable == "dswrf":
        return max(0.0, 250.0 - forecast_hour * 0.2)
    raise ValueError(f"Unsupported mock GFS variable: {variable}")


def build_mock_payload(cycle_time: datetime, variable: str, forecast_hour: int) -> dict[str, Any]:
    return {
        "source": "gfs",
        "format": "mock-grib2",
        "cycle_time": cycle_time.isoformat(),
        "variable": variable,
        "forecast_hour": forecast_hour,
        "values": [default_mock_value(variable, forecast_hour)],
        "shape": [1],
        "created_by": "workers.data_adapters.mock_gfs",
    }


def default_mock_era5_value(variable: str, forecast_hour: int) -> float:
    if variable == "2m_temperature":
        return 285.0 + forecast_hour * 0.05
    if variable == "2m_dewpoint_temperature":
        return 278.0 + forecast_hour * 0.03
    if variable == "10m_u_component_of_wind":
        return 3.0
    if variable == "10m_v_component_of_wind":
        return 4.0
    if variable == "surface_pressure":
        return 101325.0
    if variable == "total_precipitation":
        return max(0.0, forecast_hour * 0.00025)
    if variable == "surface_net_solar_radiation":
        return max(0.0, forecast_hour * 3600.0 * 180.0)
    if variable == "surface_net_thermal_radiation":
        return forecast_hour * 3600.0 * -70.0
    raise ValueError(f"Unsupported mock ERA5 variable: {variable}")


def build_mock_era5_payload(
    cycle_time: datetime,
    variable: str,
    forecast_hour: int,
    values: list[float] | tuple[float, ...] | None = None,
) -> dict[str, Any]:
    payload_values = (
        [float(value) for value in values] if values is not None else [default_mock_era5_value(variable, forecast_hour)]
    )
    return {
        "source": "ERA5",
        "format": "mock-grib2",
        "cycle_time": cycle_time.isoformat(),
        "variable": variable,
        "forecast_hour": forecast_hour,
        "values": payload_values,
        "shape": [len(payload_values)],
        "created_by": "workers.data_adapters.mock_era5",
        "metadata": {
            "product_type": "reanalysis",
            "accumulation_type": "since_midnight"
            if variable
            in {
                "total_precipitation",
                "surface_net_solar_radiation",
                "surface_net_thermal_radiation",
            }
            else "instantaneous",
        },
    }
