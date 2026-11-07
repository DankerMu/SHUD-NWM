from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from workers.canonical_converter.grib_packing import negative_noise_tolerance_mm, quantization_step_from_attrs

# IFS tp after the 12-bit packing change (#3090), as cfgrib reports it with read_keys.
SIMPLE_12BIT_ATTRS: dict[str, Any] = {
    "GRIB_packingType": "grid_simple",
    "GRIB_binaryScaleFactor": -13,
    "GRIB_decimalScaleFactor": 0,
    "GRIB_bitsPerValue": 12,
}


def _attrs(**overrides: Any) -> dict[str, Any]:
    attrs = dict(SIMPLE_12BIT_ATTRS)
    attrs.update(overrides)
    return attrs


def test_grid_simple_step_is_two_to_e_over_ten_to_d() -> None:
    assert quantization_step_from_attrs(SIMPLE_12BIT_ATTRS) == 0.0001220703125
    assert quantization_step_from_attrs(_attrs(GRIB_binaryScaleFactor=3, GRIB_decimalScaleFactor=2)) == pytest.approx(
        0.08
    )
    assert quantization_step_from_attrs(_attrs(GRIB_binaryScaleFactor=-2, GRIB_decimalScaleFactor=-1)) == pytest.approx(
        2.5
    )


def test_numpy_integer_keys_are_accepted() -> None:
    attrs = _attrs(
        GRIB_binaryScaleFactor=np.int64(-13),
        GRIB_decimalScaleFactor=np.int32(0),
        GRIB_bitsPerValue=np.int16(12),
    )
    assert quantization_step_from_attrs(attrs) == 0.0001220703125


@pytest.mark.parametrize(
    "missing_key",
    ["GRIB_packingType", "GRIB_binaryScaleFactor", "GRIB_decimalScaleFactor", "GRIB_bitsPerValue"],
)
def test_missing_key_gives_none(missing_key: str) -> None:
    attrs = dict(SIMPLE_12BIT_ATTRS)
    del attrs[missing_key]
    assert quantization_step_from_attrs(attrs) is None


@pytest.mark.parametrize("odd_value", [-13.0, "-13", None, True, np.float64(-13.0)])
def test_non_integer_binary_scale_factor_gives_none(odd_value: Any) -> None:
    assert quantization_step_from_attrs(_attrs(GRIB_binaryScaleFactor=odd_value)) is None


@pytest.mark.parametrize("key", ["GRIB_decimalScaleFactor", "GRIB_bitsPerValue"])
def test_bool_or_float_in_other_integer_keys_gives_none(key: str) -> None:
    assert quantization_step_from_attrs(_attrs(**{key: False})) is None
    assert quantization_step_from_attrs(_attrs(**{key: 12.0})) is None


@pytest.mark.parametrize("bits_per_value", [0, -1])
def test_constant_field_bits_per_value_gives_none(bits_per_value: int) -> None:
    assert quantization_step_from_attrs(_attrs(GRIB_bitsPerValue=bits_per_value)) is None


@pytest.mark.parametrize("packing_type", ["grid_ieee", "grid_complex", "grid_ccsds", "", None, 0])
def test_packing_outside_allow_list_gives_none(packing_type: Any) -> None:
    assert quantization_step_from_attrs(_attrs(GRIB_packingType=packing_type)) is None


@pytest.mark.parametrize(
    ("binary_scale", "decimal_scale"),
    [
        (5000, 0),  # 2.0**5000 overflows
        (-5000, 0),  # underflows to 0.0: not positive
        (0, 400),  # 10.0**400 overflows
        (0, -400),  # 10.0**-400 underflows to 0.0: division by zero
    ],
)
def test_extreme_scale_factors_give_none_without_raising(binary_scale: int, decimal_scale: int) -> None:
    attrs = _attrs(GRIB_binaryScaleFactor=binary_scale, GRIB_decimalScaleFactor=decimal_scale)
    assert quantization_step_from_attrs(attrs) is None


@pytest.mark.parametrize("attrs", [{}, None, "not a mapping"])
def test_empty_or_non_mapping_attrs_give_none(attrs: Any) -> None:
    assert quantization_step_from_attrs(attrs) is None


def test_tolerance_is_sum_of_known_steps_with_env_floor() -> None:
    assert negative_noise_tolerance_mm(0.1, 0.1220703125, 0.1220703125) == 0.244140625
    assert negative_noise_tolerance_mm(0.1, None, 0.1220703125) == 0.1220703125
    assert negative_noise_tolerance_mm(0.1, 0.0152587890625, 0.0152587890625) == 0.1
    assert negative_noise_tolerance_mm(0.5, 0.1220703125, 0.1220703125) == 0.5


def test_tolerance_without_known_steps_is_the_floor_itself() -> None:
    assert negative_noise_tolerance_mm(0.1) == 0.1
    assert negative_noise_tolerance_mm(0.1, None, None) == 0.1
    assert negative_noise_tolerance_mm(-1.0, None, None) == -1.0
