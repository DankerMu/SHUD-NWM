"""GRIB packing quantisation step and the de-accumulation noise tolerance derived from it (#3090).

A simple-packed GRIB value is stored as ``R + X * 2^E / 10^D``, so two adjacent
representable values differ by the quantisation step ``2^E / 10^D`` (native units)
and each decoded value carries a rounding error of at most half a step. The reader
asks cfgrib for the packing keys through ``read_keys``; cfgrib then exposes them as
``GRIB_<key>`` attrs on the data array.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping
from typing import Any

GRIB_PACKING_READ_KEYS: tuple[str, ...] = (
    "binaryScaleFactor",
    "decimalScaleFactor",
    "bitsPerValue",
    "packingType",
)

# Measured allow-list: only simple packing has the plain 2^E / 10^D step. Other
# packings (ieee, complex, ccsds, ...) are not measured and contribute no step.
QUANTIZED_PACKING_TYPES = frozenset({"grid_simple"})


def _integer_attr(attrs: Mapping[str, Any], key: str) -> int | None:
    value = attrs.get(key)
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        return None
    return int(value)


def quantization_step_from_attrs(attrs: Mapping[str, Any]) -> float | None:
    """Quantisation step ``2^E / 10^D`` of a cfgrib data array, or ``None`` when it is not known.

    ``None`` for a missing or non-integer key, a constant field (``bitsPerValue <= 0``:
    exact, no quantisation), a packing type outside the allow-list, or a result that
    is not finite and positive. Never raises.
    """
    try:
        if attrs.get("GRIB_packingType") not in QUANTIZED_PACKING_TYPES:
            return None
        binary_scale = _integer_attr(attrs, "GRIB_binaryScaleFactor")
        decimal_scale = _integer_attr(attrs, "GRIB_decimalScaleFactor")
        bits_per_value = _integer_attr(attrs, "GRIB_bitsPerValue")
        if binary_scale is None or decimal_scale is None or bits_per_value is None:
            return None
        if bits_per_value <= 0:
            return None
        step = 2.0**binary_scale / 10.0**decimal_scale
    except (AttributeError, TypeError, OverflowError, ZeroDivisionError):
        return None
    if not math.isfinite(step) or step <= 0.0:
        return None
    return step


def negative_noise_tolerance_mm(floor_mm: float, *steps_mm: float | None) -> float:
    """``max(floor_mm, sum of the known steps)``.

    A difference of two quantised values can miss by up to ``(s_prev + s_curr) / 2``;
    the sum gives a 2x margin, so a pure one-step artefact (``delta == -step`` on
    equal steps) stays below the ``delta <= -tolerance`` significance test. With no
    known step the floor is returned unchanged, whatever its value.
    """
    known_steps = [step for step in steps_mm if step is not None]
    if not known_steps:
        return floor_mm
    return max(floor_mm, sum(known_steps))
