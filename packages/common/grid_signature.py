"""Shared grid-signature algorithm for producer and registry.

This module owns the single canonical algorithm for computing a grid's
``grid_signature`` from an ordered sequence of grid cells. Both the forcing
producer (`workers/forcing_producer/producer.py`) and the grid registry MUST
import from this module; independent reimplementation is forbidden by the
canonical-source-grid-registry OpenSpec change (see spec scenario
"Both producer and registry import the single shared helper").

The signature is SHA-256 over ordered ``(grid_cell_id, round(lon,12),
round(lat,12))`` tuples wrapped as ``{"grid_points": [...]}`` and serialized
via ``json.dumps(sort_keys=True, separators=(",", ":"))``. The serialization
helpers ``_json_bytes`` and ``_json_default`` are exposed for producer
re-export so the 8 non-signature producer call sites continue to resolve to
this module's implementations.

Public canonical helpers
------------------------
* :data:`COORDINATE_ROUNDING_DECIMALS` — the single shared 12-decimal
  rounding rule used by the signature and by
  :mod:`workers.mapping_builder.binding` (§4.2, docs §7.3). Callers MUST
  import this constant rather than hand-copying the literal ``12``.
* :func:`canonical_json_bytes` — public alias for :func:`_json_bytes` so
  external callers (e.g. the mapping builder's binding-artifact emitter,
  Epic #909 SUB-11) can serialize payloads via the single shared authority
  and never hand-roll their own ``json.dumps(sort_keys=True, ...)`` call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from packages.common.object_store import sha256_bytes

#: Shared 12-decimal rounding precision for grid signature tuples and any
#: downstream binding coordinate comparison (see docs §7.3 and Epic #909
#: SUB-11 §4.2). Promoted to a public constant so callers never hand-copy
#: the literal ``12``.
COORDINATE_ROUNDING_DECIMALS: int = 12


class GridPoint(Protocol):
    """Structural protocol for grid cells consumed by the signature helper.

    Any object exposing these three attributes can be hashed. The producer's
    ``@dataclass(frozen=True) GridPoint`` satisfies this protocol without a
    nominal dependency.
    """

    grid_cell_id: str
    longitude: float
    latitude: float


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            normalized = value.replace(tzinfo=UTC)
        else:
            normalized = value.astimezone(UTC)
        return normalized.isoformat().replace("+00:00", "Z")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable.")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize ``payload`` to canonical JSON bytes.

    Public authority alias for :func:`_json_bytes`. Callers such as the
    mapping builder's binding-artifact emitter (Epic #909 SUB-11) MUST use
    this helper instead of hand-rolling ``json.dumps(sort_keys=True, ...)``
    so a datetime or other non-JSON-native value serializes via the shared
    :func:`_json_default` rather than crashing or silently diverging.
    """
    return _json_bytes(payload)


def grid_signature_tuples(grid_points: Sequence[GridPoint]) -> tuple[tuple[str, float, float], ...]:
    """Return the ordered ``(grid_cell_id, round(lon,12), round(lat,12))`` tuples."""
    return tuple(
        (
            point.grid_cell_id,
            round(float(point.longitude), COORDINATE_ROUNDING_DECIMALS),
            round(float(point.latitude), COORDINATE_ROUNDING_DECIMALS),
        )
        for point in grid_points
    )


def grid_signature_hash(grid_points: Sequence[GridPoint]) -> str:
    """Return the SHA-256 hex digest of the canonical JSON envelope."""
    return sha256_bytes(_json_bytes({"grid_points": grid_signature_tuples(grid_points)}))
