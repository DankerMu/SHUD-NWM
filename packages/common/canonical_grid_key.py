"""Source-agnostic ``canonical_grid_key`` derivation for the grid registry.

This module owns the single canonical algorithm for deriving a snapshot's
``canonical_grid_key`` from the three-input identity ``(grid_signature, bbox,
native_resolution)``. The Task 3.1b registry writer, the Task 5.1 shared-
binding eligibility path, and the Task 3.3 backfill integration test all
import from here; independent reimplementation is forbidden by the
canonical-source-grid-registry OpenSpec change (see Task 3.1c contract).

The key is SHA-256 over a compact-JSON envelope with the ``native_resolution``
canonicalized to a 12-decimal string so ``0.25`` vs ``0.2500000001`` never
silently collide on the same key across two independent implementations.
``source_id`` is deliberately NOT an input; sharing eligibility keys on
signature equality across sources.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping

from packages.common.object_store import sha256_bytes

_BBOX_KEYS: frozenset[str] = frozenset({"south", "north", "west", "east"})


def derive_canonical_grid_key(
    grid_signature: str,
    bbox: Mapping[str, float],
    native_resolution: float,
) -> str:
    """Return the 64-char lowercase hex ``canonical_grid_key`` for one snapshot.

    Parameters
    ----------
    grid_signature:
        Lowercase-hex SHA-256 digest returned by
        :func:`packages.common.grid_signature.grid_signature_hash`. MUST be
        exactly 64 characters and match ``[0-9a-f]``; other inputs fail closed.
    bbox:
        Four-corner bounding box with keys exactly ``{"south", "north",
        "west", "east"}``, each a Python ``float``. Extra or missing keys or
        non-float values fail closed naming the offending key.
    native_resolution:
        Grid axis spacing in degrees. Must be a finite ``float`` (``NaN`` and
        ``inf`` fail closed). Canonicalized to ``f"{native_resolution:.12f}"``
        before hashing so representationally-equal values are byte-equal.

    Raises
    ------
    ValueError
        On any contract violation, with a message naming the offending field.
    """
    _validate_grid_signature(grid_signature)
    _validate_native_resolution(native_resolution)
    validated_bbox = _validate_bbox(bbox)

    payload = json.dumps(
        {
            "grid_signature": grid_signature,
            "bbox": {
                "south": validated_bbox["south"],
                "north": validated_bbox["north"],
                "west": validated_bbox["west"],
                "east": validated_bbox["east"],
            },
            "native_resolution": f"{native_resolution:.12f}",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def _validate_grid_signature(grid_signature: str) -> None:
    if not isinstance(grid_signature, str):
        raise ValueError(
            f"grid_signature must be a 64-char lowercase hex string; got type {type(grid_signature).__name__}."
        )
    if len(grid_signature) != 64:
        raise ValueError(
            f"grid_signature must be exactly 64 characters; got length {len(grid_signature)}."
        )
    for char in grid_signature:
        if char not in "0123456789abcdef":
            raise ValueError(
                "grid_signature must contain only lowercase hex characters [0-9a-f]; "
                f"found offending character {char!r}."
            )


def _validate_native_resolution(native_resolution: float) -> None:
    if not isinstance(native_resolution, (int, float)) or isinstance(native_resolution, bool):
        raise ValueError(
            f"native_resolution must be a float; got type {type(native_resolution).__name__}."
        )
    value = float(native_resolution)
    if math.isnan(value) or math.isinf(value):
        raise ValueError(
            f"native_resolution must be finite; got {native_resolution!r}."
        )


def _validate_bbox(bbox: Mapping[str, float]) -> Mapping[str, float]:
    if not isinstance(bbox, Mapping):
        raise ValueError(
            f"bbox must be a Mapping[str, float]; got type {type(bbox).__name__}."
        )
    provided_keys = set(bbox.keys())
    missing = _BBOX_KEYS - provided_keys
    if missing:
        offending = sorted(missing)[0]
        raise ValueError(
            f"bbox is missing required key {offending!r}; expected keys are "
            f"{sorted(_BBOX_KEYS)}."
        )
    extra = provided_keys - _BBOX_KEYS
    if extra:
        offending = sorted(extra)[0]
        raise ValueError(
            f"bbox has unexpected key {offending!r}; expected keys are "
            f"{sorted(_BBOX_KEYS)}."
        )
    for key in _BBOX_KEYS:
        value = bbox[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"bbox[{key!r}] must be a float; got type {type(value).__name__}."
            )
        float_value = float(value)
        if math.isnan(float_value) or math.isinf(float_value):
            raise ValueError(
                f"bbox[{key!r}] must be finite; got {value!r}."
            )
    return bbox
