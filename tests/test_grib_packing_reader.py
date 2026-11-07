"""#3090: the raw reader derives the GRIB quantisation step from a real GRIB2 message.

`grib` lane: the messages are encoded at test time with the runtime's ecCodes and
decoded through cfgrib. `tests.grib2_fixture_support` is imported inside the test
bodies because pure CI has no ecCodes library (#2700).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from packages.common.test_netcdf4 import encode_test_netcdf4
from workers.canonical_converter.converter import (
    IFSCanonicalConverter,
    IFSCanonicalConverterConfig,
    parse_cycle_time,
)

CONVERTER_LOGGER = "workers.canonical_converter.converter"
CYCLE = "2026102012"


def _converter(tmp_path: Path) -> IFSCanonicalConverter:
    return IFSCanonicalConverter(
        config=IFSCanonicalConverterConfig(workspace_root=tmp_path),
        repository=None,
        object_store=LocalObjectStore(tmp_path),
    )


def _write_tp(converter: IFSCanonicalConverter, payload: bytes, *, suffix: str) -> dict[str, Any]:
    local_key = f"raw/IFS/{CYCLE}/ifs.t12z.f051.tp.{suffix}"
    converter.object_store.write_bytes_atomic(local_key, payload)
    return {"local_key": local_key, "variable": "tp", "forecast_hour": 51, "metadata": {}}


def _encode_tp(values: list[float]) -> bytes:
    from tests.grib2_fixture_support import encode_grib2_field

    return encode_grib2_field(
        "tp",
        values,
        bits_per_value=12,
        cycle_time=parse_cycle_time(CYCLE),
        forecast_hour=51,
        centre="ecmf",
        longitude=100.0,
        latitude=30.25,
    )


def _assert_no_netcdf4_fallback(caplog: pytest.LogCaptureFixture) -> None:
    fallbacks = [record for record in caplog.records if "falling back to netcdf4" in record.getMessage()]
    assert fallbacks == []


@pytest.mark.grib
def test_12bit_tp_message_yields_eccodes_quantization_step(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tests.grib2_fixture_support import eccodes_version, grib2_packing_keys

    print(f"ecCodes {eccodes_version()}")
    converter = _converter(tmp_path)
    tp_m = [0.0, 0.0012, 0.0057, 0.0123, 0.0201, 0.0345]
    entry = _write_tp(converter, _encode_tp(tp_m), suffix="grib2")

    (packing,) = grib2_packing_keys(converter.object_store.resolve_path(entry["local_key"]))
    assert packing["packingType"] == "grid_simple"
    assert packing["bitsPerValue"] == 12

    with caplog.at_level(logging.WARNING, logger=CONVERTER_LOGGER):
        record = converter._read_record_with_xarray(entry)
    _assert_no_netcdf4_fallback(caplog)

    expected_step = 2.0 ** packing["binaryScaleFactor"] / 10.0 ** packing["decimalScaleFactor"]
    assert record.quantization_step == expected_step
    assert record.quantization_step > 0.0
    assert len(record.values) == len(tp_m)
    # Simple packing rounds each value to the nearest step: error <= step / 2.
    for decoded, original in zip(record.values, tp_m, strict=True):
        assert abs(decoded - original) <= expected_step / 2.0 + 1e-12
    # The step lives in its own field; the manifest entry metadata passes through untouched.
    assert dict(record.metadata) == {}


@pytest.mark.grib
def test_constant_tp_field_has_no_quantization_step(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tests.grib2_fixture_support import grib2_packing_keys

    converter = _converter(tmp_path)
    entry = _write_tp(converter, _encode_tp([0.0] * 4), suffix="grib2")

    (packing,) = grib2_packing_keys(converter.object_store.resolve_path(entry["local_key"]))
    # ecCodes encodes a constant field with zero bits: exact, no quantisation.
    assert packing["packingType"] == "grid_simple"
    assert packing["bitsPerValue"] == 0

    with caplog.at_level(logging.WARNING, logger=CONVERTER_LOGGER):
        record = converter._read_record_with_xarray(entry)
    _assert_no_netcdf4_fallback(caplog)

    assert record.values == (0.0, 0.0, 0.0, 0.0)
    assert record.quantization_step is None


@pytest.mark.grib
def test_netcdf4_fallback_record_has_no_quantization_step(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    converter = _converter(tmp_path)
    payload = encode_test_netcdf4("tp", 51, values=[0.002], cycle_time=parse_cycle_time(CYCLE), source="IFS")
    entry = _write_tp(converter, payload, suffix="nc")

    with caplog.at_level(logging.WARNING, logger=CONVERTER_LOGGER):
        record = converter._read_record_with_xarray(entry)

    assert any("falling back to netcdf4" in item.getMessage() for item in caplog.records)
    assert record.values == pytest.approx((0.002,))
    assert record.quantization_step is None
