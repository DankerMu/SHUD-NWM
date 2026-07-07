from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from packages.common.grid_signature import (
    _json_bytes,
    _json_default,
    grid_signature_hash,
    grid_signature_tuples,
)
from workers.forcing_producer import producer as producer_module


@dataclass(frozen=True)
class _StubGridPoint:
    """Structural stub for the ``GridPoint`` protocol used by the shared helper.

    Uses a local class to prove the helper's API is structural, not nominal
    (i.e. it does not depend on ``workers.forcing_producer.GridPoint``).
    """

    grid_cell_id: str
    longitude: float
    latitude: float


def _fixture_grid_points() -> list[_StubGridPoint]:
    return [
        _StubGridPoint(grid_cell_id="0", longitude=63.0, latitude=8.0),
        _StubGridPoint(grid_cell_id="1", longitude=63.25, latitude=8.0),
        _StubGridPoint(grid_cell_id="2", longitude=63.5, latitude=8.25),
        _StubGridPoint(grid_cell_id="3", longitude=-179.999999999999, latitude=-89.999999999999),
    ]


def test_shared_hash_matches_producer_symbol_byte_for_byte() -> None:
    """Task 1.1 Evidence Floor: shared_hash == producer._grid_signature_hash(fixture)."""
    fixture = _fixture_grid_points()
    shared_hash = grid_signature_hash(fixture)
    assert shared_hash == producer_module._grid_signature_hash(fixture)


def test_grid_signature_tuples_rounds_to_12_decimals() -> None:
    unrounded = _StubGridPoint(
        grid_cell_id="cell",
        longitude=1.1234567890123456,
        latitude=-2.9876543210987654,
    )
    tuples = grid_signature_tuples([unrounded])
    assert tuples == (("cell", round(1.1234567890123456, 12), round(-2.9876543210987654, 12)),)
    # Sanity check: round(x, 12) truncates the 13th decimal onward.
    _, lon, lat = tuples[0]
    assert lon == 1.123456789012
    assert lat == -2.987654321099


def test_grid_signature_hash_is_cell_order_sensitive() -> None:
    a = _StubGridPoint(grid_cell_id="0", longitude=10.0, latitude=20.0)
    b = _StubGridPoint(grid_cell_id="1", longitude=30.0, latitude=40.0)
    assert grid_signature_hash([a, b]) != grid_signature_hash([b, a])


def test_json_envelope_shape_and_prefix() -> None:
    fixture = _fixture_grid_points()
    envelope = _json_bytes({"grid_points": grid_signature_tuples(fixture)})
    # Compact separators + sort_keys render {"grid_points":[ ... without spaces.
    assert envelope.startswith(b'{"grid_points":[')
    decoded = json.loads(envelope.decode("utf-8"))
    assert list(decoded.keys()) == ["grid_points"]
    assert len(decoded["grid_points"]) == len(fixture)
    for entry, point in zip(decoded["grid_points"], fixture, strict=True):
        assert len(entry) == 3
        cell_id, lon, lat = entry
        assert isinstance(cell_id, str)
        assert isinstance(lon, float)
        assert isinstance(lat, float)
        assert cell_id == point.grid_cell_id
        assert lon == round(point.longitude, 12)
        assert lat == round(point.latitude, 12)


def test_producer_signature_symbols_resolve_to_shared_module() -> None:
    """Static-import binding: prove producer re-exports the shared symbols."""
    assert producer_module._grid_signature_hash.__module__ == "packages.common.grid_signature"
    assert producer_module._grid_signature.__module__ == "packages.common.grid_signature"


def test_json_default_rejects_unsupported_types() -> None:
    with pytest.raises(TypeError, match="Object of type set is not JSON serializable."):
        _json_default({1, 2, 3})
