"""GRIB2 fixtures for the `grib` lane, encoded at test time with the installed ecCodes.

Nothing is checked in: a GRIB sample pins one ecCodes version's tables, while the
lane exists to prove decode against the runtime's own version (#2700).

`eccodes` is imported inside the functions, and callers import this module inside
their test bodies: pure CI imports every test module without the ecCodes library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path


def eccodes_version() -> str:
    import eccodes

    return str(eccodes.codes_get_api_version())


def encode_grib2_bundle(
    values_by_short_name: Mapping[str, float],
    *,
    cycle_time: datetime,
    forecast_hour: int,
    centre: str,
    longitude: float,
    latitude: float,
) -> bytes:
    """One single-point GRIB2 message per shortName, concatenated in mapping order.

    Accumulated-product templates (`stepType`/`stepRange`) are not modelled: the
    converter selects messages by shortName only.
    """
    import eccodes

    messages: list[bytes] = []
    for short_name, value in values_by_short_name.items():
        handle = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
        try:
            eccodes.codes_set(handle, "centre", centre)
            eccodes.codes_set(handle, "Ni", 1)
            eccodes.codes_set(handle, "Nj", 1)
            eccodes.codes_set(handle, "latitudeOfFirstGridPointInDegrees", latitude)
            eccodes.codes_set(handle, "latitudeOfLastGridPointInDegrees", latitude)
            eccodes.codes_set(handle, "longitudeOfFirstGridPointInDegrees", longitude)
            eccodes.codes_set(handle, "longitudeOfLastGridPointInDegrees", longitude)
            eccodes.codes_set(handle, "iDirectionIncrementInDegrees", 0.25)
            eccodes.codes_set(handle, "jDirectionIncrementInDegrees", 0.25)
            eccodes.codes_set(handle, "dataDate", int(cycle_time.strftime("%Y%m%d")))
            eccodes.codes_set(handle, "dataTime", cycle_time.hour * 100)
            eccodes.codes_set(handle, "shortName", short_name)
            eccodes.codes_set(handle, "step", forecast_hour)
            eccodes.codes_set(handle, "bitsPerValue", 32)
            eccodes.codes_set_values(handle, [float(value)])
            # Concept resolution is the version-dependent part: a shortName this
            # ecCodes cannot resolve must not pass as some other parameter.
            encoded_short_name = eccodes.codes_get(handle, "shortName")
            if encoded_short_name != short_name:
                raise AssertionError(
                    f"ecCodes {eccodes_version()} encoded shortName {short_name!r} as {encoded_short_name!r}"
                )
            messages.append(eccodes.codes_get_message(handle))
        finally:
            eccodes.codes_release(handle)
    return b"".join(messages)


def encode_grib2_field(
    short_name: str,
    values: Sequence[float],
    *,
    bits_per_value: int,
    cycle_time: datetime,
    forecast_hour: int,
    centre: str,
    longitude: float,
    latitude: float,
    ni: int = 2,
) -> bytes:
    """One multi-point, simple-packed GRIB2 message on a regular 0.25-degree grid.

    ``values`` fill the grid row by row, ``ni`` points per row (``len(values) / ni``
    rows, north to south from ``latitude``). ``bits_per_value`` is the requested
    packing width: ecCodes derives the binary/decimal scale factors from it and the
    data range, and encodes a constant field with ``bitsPerValue == 0``.
    """
    import eccodes

    if ni <= 0 or len(values) % ni:
        raise ValueError(f"{len(values)} values do not fill rows of {ni} points")
    nj = len(values) // ni
    handle = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(handle, "centre", centre)
        eccodes.codes_set(handle, "Ni", ni)
        eccodes.codes_set(handle, "Nj", nj)
        eccodes.codes_set(handle, "latitudeOfFirstGridPointInDegrees", latitude)
        eccodes.codes_set(handle, "latitudeOfLastGridPointInDegrees", latitude - 0.25 * (nj - 1))
        eccodes.codes_set(handle, "longitudeOfFirstGridPointInDegrees", longitude)
        eccodes.codes_set(handle, "longitudeOfLastGridPointInDegrees", longitude + 0.25 * (ni - 1))
        eccodes.codes_set(handle, "iDirectionIncrementInDegrees", 0.25)
        eccodes.codes_set(handle, "jDirectionIncrementInDegrees", 0.25)
        eccodes.codes_set(handle, "dataDate", int(cycle_time.strftime("%Y%m%d")))
        eccodes.codes_set(handle, "dataTime", cycle_time.hour * 100)
        eccodes.codes_set(handle, "shortName", short_name)
        eccodes.codes_set(handle, "step", forecast_hour)
        eccodes.codes_set(handle, "packingType", "grid_simple")
        eccodes.codes_set(handle, "bitsPerValue", bits_per_value)
        eccodes.codes_set_values(handle, [float(value) for value in values])
        encoded_short_name = eccodes.codes_get(handle, "shortName")
        if encoded_short_name != short_name:
            raise AssertionError(
                f"ecCodes {eccodes_version()} encoded shortName {short_name!r} as {encoded_short_name!r}"
            )
        return eccodes.codes_get_message(handle)
    finally:
        eccodes.codes_release(handle)


def grib2_packing_keys(path: Path) -> list[dict[str, int | str]]:
    """Packing keys of every message of a GRIB file, in file order, read with ecCodes."""
    import eccodes

    packing: list[dict[str, int | str]] = []
    with path.open("rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream)
            if handle is None:
                return packing
            try:
                packing.append(
                    {
                        "packingType": str(eccodes.codes_get(handle, "packingType")),
                        "bitsPerValue": int(eccodes.codes_get(handle, "bitsPerValue")),
                        "binaryScaleFactor": int(eccodes.codes_get(handle, "binaryScaleFactor")),
                        "decimalScaleFactor": int(eccodes.codes_get(handle, "decimalScaleFactor")),
                    }
                )
            finally:
                eccodes.codes_release(handle)


def grib2_short_names(path: Path) -> list[str]:
    """shortName of every message of a GRIB file, in file order, read with ecCodes."""
    import eccodes

    short_names: list[str] = []
    with path.open("rb") as stream:
        while True:
            handle = eccodes.codes_grib_new_from_file(stream)
            if handle is None:
                return short_names
            try:
                short_names.append(str(eccodes.codes_get(handle, "shortName")))
            finally:
                eccodes.codes_release(handle)
