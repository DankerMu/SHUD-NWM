"""GRIB2 fixtures for the `grib` lane, encoded at test time with the installed ecCodes.

Nothing is checked in: a GRIB sample pins one ecCodes version's tables, while the
lane exists to prove decode against the runtime's own version (#2700).

`eccodes` is imported inside the functions, and callers import this module inside
their test bodies: pure CI imports every test module without the ecCodes library.
"""

from __future__ import annotations

from collections.abc import Mapping
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
