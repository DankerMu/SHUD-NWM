"""NetCDF4 test fixture utilities — replaces mock_grib for test data generation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

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

GFS_VARIABLES: tuple[str, ...] = (
    "tmp2m",
    "apcp",
    "rh2m",
    "u10m",
    "v10m",
    "pressfc",
    "dswrf",
)

CFGRIB_SHORT_NAMES: dict[str, str] = {
    "tmp2m": "t2m",
    "apcp": "tp",
    "rh2m": "r2",
    "u10m": "u10",
    "v10m": "v10",
    "pressfc": "sp",
    "dswrf": "sdswrf",
    "2m_temperature": "2t",
    "2m_dewpoint_temperature": "2d",
    "10m_u_component_of_wind": "10u",
    "10m_v_component_of_wind": "10v",
    "surface_pressure": "sp",
    "total_precipitation": "tp",
    "surface_net_solar_radiation": "ssr",
    "surface_net_thermal_radiation": "str",
}


def default_gfs_value(variable: str, forecast_hour: int) -> float:
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
    raise ValueError(f"Unsupported GFS variable: {variable}")


def default_era5_value(variable: str, forecast_hour: int) -> float:
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
    raise ValueError(f"Unsupported ERA5 variable: {variable}")


def write_test_netcdf4(
    path: str | Path,
    variable: str,
    forecast_hour: int,
    values: list[float] | None = None,
    cycle_time: datetime | None = None,
    source: str = "gfs",
) -> bytes:
    """Write a minimal NetCDF4 file for testing. Returns the file content as bytes."""
    import xarray as xr

    short_name = CFGRIB_SHORT_NAMES.get(variable, variable)
    if values is None:
        if source == "ERA5":
            values = [default_era5_value(variable, forecast_hour)]
        else:
            values = [default_gfs_value(variable, forecast_hour)]

    ds = xr.Dataset(
        {short_name: (["point"], values)},
        coords={"point": list(range(len(values)))},
        attrs={
            "source": source,
            "variable": variable,
            "forecast_hour": forecast_hour,
            "GRIB_shortName": short_name,
        },
    )
    if cycle_time is not None:
        ds.attrs["cycle_time"] = cycle_time.isoformat()
    ds[short_name].attrs["GRIB_shortName"] = short_name
    ds[short_name].attrs["shortName"] = short_name

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(target, engine="netcdf4")
    content = target.read_bytes()
    ds.close()
    return content


def encode_test_netcdf4(
    variable: str,
    forecast_hour: int,
    values: list[float] | None = None,
    cycle_time: datetime | None = None,
    source: str = "gfs",
) -> bytes:
    """Encode a NetCDF4 payload in memory (returns bytes without needing a file path)."""
    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as tmp:
        path = P(tmp) / "data.nc"
        return write_test_netcdf4(path, variable, forecast_hour, values, cycle_time, source)
