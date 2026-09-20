"""#2165 the national discharge feature budget, #2166 observable tile blanking.

Partition of ``tests/test_hydro_display_mvt_scaling.py`` (#2074).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from apps.api.errors import ApiError
from apps.api.routes import hydro_display, hydro_display_postgis
from services.tiles import mvt as mvt_module
from services.tiles.mvt import MVT_MAX_COORDINATES
from tests.hydro_display_mvt_helpers import (
    _TILE_ROUTE_LOGGER,
    _assert_one_blanked_record,
    _blanked_records,
    _budget_row,
    _keep_fixed_instant_fixtures_inside_the_cycle_lookback,  # noqa: F401
    _Session,
    _truncation_records,
)

# ---------------------------------------------------------------------------
# #2165: the national discharge layer's own feature budget, and #2166: a tile
# blanked by a per-feature overflow is an observable event. Appended at the END,
# for the reason stated above the #2009 round-4 block.
# ---------------------------------------------------------------------------

# The value design D4 / the `postgis-tile-clipping-cache` spec name, spelled as a
# literal so it is not read back from the module under test.
_NATIONAL_DISCHARGE_FEATURE_LIMIT = 20_000
_GLOBAL_FEATURE_LIMIT = 10_000
_LAYER_PARAMS: dict[str, dict[str, Any]] = {
    "river-network-national": {},
    "river-network": {"basin_version_id": "bv_a"},
    "hydro": {"variable": "q_down"},
    "hydro-national": {"variable": "q_down"},
    "met-stations": {"basin_version_id": "bv_a"},
}


def test_feature_limit_is_raised_only_for_the_national_discharge_layer() -> None:
    assert mvt_module.feature_limit("hydro-national") == _NATIONAL_DISCHARGE_FEATURE_LIMIT
    # The other four layers, and a caller that names no layer, keep the global
    # budget -- whose value this change does not move.
    assert mvt_module.MVT_MAX_FEATURES == _GLOBAL_FEATURE_LIMIT
    for layer in ("river-network", "river-network-national", "hydro", "met-stations", None):
        assert mvt_module.feature_limit(layer) == mvt_module.MVT_MAX_FEATURES, layer


def test_postgis_tile_params_bind_each_layer_its_own_feature_limit() -> None:
    expected = {layer: _GLOBAL_FEATURE_LIMIT for layer in _LAYER_PARAMS}
    expected["hydro-national"] = _NATIONAL_DISCHARGE_FEATURE_LIMIT

    for layer, value in expected.items():
        assert hydro_display._postgis_tile_params({}, z=3, x=6, y=3, layer=layer)["feature_limit"] == value, layer
    # Script callers pass no layer and compare the exact binding dictionary.
    assert hydro_display._postgis_tile_params({}, z=3, x=6, y=3)["feature_limit"] == _GLOBAL_FEATURE_LIMIT


def test_production_tile_bind_site_binds_the_layer_feature_limit(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")

    for layer, params in _LAYER_PARAMS.items():
        session = _Session([_budget_row(10)])

        assert hydro_display._fetch_postgis_tile_bytes(session, layer, params, z=3, x=6, y=3) == b"pbf-bytes"

        expected = _NATIONAL_DISCHARGE_FEATURE_LIMIT if layer == "hydro-national" else _GLOBAL_FEATURE_LIMIT
        assert session.executions[0][1]["feature_limit"] == expected, layer


def test_national_discharge_tile_above_the_old_limit_is_served_whole_and_silent(
    monkeypatch: Any, caplog: Any
) -> None:
    """13,770 intersecting features were measured on node-27; 15,000 is above 10,000 too."""
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    row = {**_budget_row(30_000), "feature_count": 15_000, "intersecting_feature_count": 15_000}
    session = _Session([row])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(session, "hydro-national", {"variable": "q_down"}, z=0, x=0, y=0)

    assert tile == b"pbf-bytes"
    assert _truncation_records(caplog) == []
    assert _blanked_records(caplog) == []


def test_national_discharge_truncation_reports_the_layer_feature_limit(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    row = {**_budget_row(40_000), "feature_count": 20_000, "intersecting_feature_count": 21_000}
    session = _Session([row])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(session, "hydro-national", {"variable": "q_down"}, z=0, x=0, y=0)

    assert tile == b"pbf-bytes"
    records = _truncation_records(caplog)
    assert len(records) == 1
    assert records[0].max_features == _NATIONAL_DISCHARGE_FEATURE_LIMIT
    assert records[0].max_features == session.executions[0][1]["feature_limit"]
    assert "feature_count=20000/21000 max_features=20000" in records[0].getMessage()


@pytest.mark.parametrize(
    ("layer", "feature_count", "max_features"),
    (
        pytest.param("hydro-national", 20_001, _NATIONAL_DISCHARGE_FEATURE_LIMIT, id="hydro-national"),
        pytest.param("river-network", 10_001, _GLOBAL_FEATURE_LIMIT, id="river-network"),
        pytest.param("hydro", 10_001, _GLOBAL_FEATURE_LIMIT, id="hydro"),
    ),
)
def test_feature_budget_413_uses_the_layer_feature_limit(
    monkeypatch: Any, layer: str, feature_count: int, max_features: int
) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    row = {**_budget_row(10), "feature_count": feature_count, "intersecting_feature_count": feature_count}
    session = _Session([row])

    with pytest.raises(ApiError) as excinfo:
        hydro_display._fetch_postgis_tile_bytes(session, layer, _LAYER_PARAMS[layer], z=3, x=6, y=3)

    assert excinfo.value.status_code == 413
    assert excinfo.value.code == "MVT_TILE_BUDGET_EXCEEDED"
    assert excinfo.value.details["max_features"] == max_features
    assert excinfo.value.details["max_features"] == session.executions[0][1]["feature_limit"]
    assert excinfo.value.details["feature_count"] == feature_count


def _overflow_row(**overrides: Any) -> dict[str, Any]:
    """The row shape a per-feature overflow really produces.

    Either overflow counter empties the shared `budget_gate`, so nothing is
    selected: `tile` is NULL (ST_AsMVT over zero rows) and the selected counts are
    0, while `prefilter_stats` still reports what intersected.
    """
    return {
        "tile": None,
        "feature_count": 0,
        "coordinate_count": 0,
        "source_identity_count": 1,
        "invalid_property_count": 0,
        "invalid_properties": "",
        "intersecting_feature_count": 23,
        "intersecting_coordinate_count": 86_160,
        "feature_coordinate_overflow_count": 0,
        "feature_coordinate_count": 12,
        "coordinate_dimension_overflow_count": 0,
        "coordinate_dimension_count": 2,
        **overrides,
    }


@pytest.mark.parametrize("layer", tuple(_LAYER_PARAMS))
def test_per_feature_coordinate_overflow_blanks_the_tile_observably(
    monkeypatch: Any, caplog: Any, layer: str
) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_overflow_row(feature_coordinate_overflow_count=1, feature_coordinate_count=60_000)])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(session, layer, _LAYER_PARAMS[layer], z=3, x=6, y=3)

    # HTTP 200 semantics unchanged: empty bytes, no error.
    assert tile == b""
    assert _truncation_records(caplog) == []
    _assert_one_blanked_record(
        caplog,
        session,
        layer,
        feature_coordinate_overflow_count=1,
        feature_coordinate_count=60_000,
        coordinate_dimension_overflow_count=0,
        coordinate_dimension_count=2,
    )
    assert session.executions[0][1]["feature_coordinate_limit"] == MVT_MAX_COORDINATES
    assert session.executions[0][1]["max_coordinate_dimensions"] == 3


def test_coordinate_dimension_overflow_blanks_the_tile_observably(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_overflow_row(coordinate_dimension_overflow_count=1, coordinate_dimension_count=4)])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(
            session, "hydro-national", {"variable": "q_down"}, z=3, x=6, y=3
        )

    assert tile == b""
    assert _truncation_records(caplog) == []
    _assert_one_blanked_record(
        caplog,
        session,
        "hydro-national",
        feature_coordinate_overflow_count=0,
        feature_coordinate_count=12,
        coordinate_dimension_overflow_count=1,
        coordinate_dimension_count=4,
    )


def test_blanked_record_reports_the_limit_actually_bound(monkeypatch: Any, caplog: Any) -> None:
    """The maxima come from the query's bind, so a lowered limit is what gets reported."""
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(hydro_display_postgis, "MVT_MAX_COORDINATES", 100)
    session = _Session([_overflow_row(feature_coordinate_overflow_count=2, feature_coordinate_count=640)])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        hydro_display._fetch_postgis_tile_bytes(session, "river-network-national", {}, z=3, x=6, y=3)

    assert session.executions[0][1]["feature_coordinate_limit"] == 100
    _assert_one_blanked_record(
        caplog,
        session,
        "river-network-national",
        feature_coordinate_overflow_count=2,
        feature_coordinate_count=640,
        coordinate_dimension_overflow_count=0,
        coordinate_dimension_count=2,
    )
    assert _blanked_records(caplog)[0].max_feature_coordinates == 100


def test_no_overflow_emits_no_blanked_record(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        for layer, params in _LAYER_PARAMS.items():
            empty = _Session([_overflow_row(intersecting_feature_count=0, intersecting_coordinate_count=0)])
            assert hydro_display._fetch_postgis_tile_bytes(empty, layer, params, z=3, x=6, y=3) == b""
            full = _Session([_budget_row(10)])
            assert hydro_display._fetch_postgis_tile_bytes(full, layer, params, z=3, x=6, y=3) == b"pbf-bytes"

    assert _blanked_records(caplog) == []


def test_overflow_on_a_424_identity_raises_before_any_blanked_record(monkeypatch: Any, caplog: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_overflow_row(source_identity_count=0, feature_coordinate_overflow_count=1)])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER), pytest.raises(ApiError) as excinfo:
        hydro_display._fetch_postgis_tile_bytes(session, "hydro-national", {"variable": "q_down"}, z=3, x=6, y=3)

    assert excinfo.value.status_code == 424
    assert _blanked_records(caplog) == []
