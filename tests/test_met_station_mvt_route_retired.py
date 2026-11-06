"""#2773 item 4: the station vector-tile route is retired.

`GET /api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf` listed
`met.met_station` rows with `active_flag = true`. Every Direct Grid station is
`active_flag = false`, so on node-27 it answered 404
`MVT_SOURCE_IDENTITY_NOT_FOUND` for every dg basin, and nothing requested it (the
frontend draws stations from `/api/v1/met/stations`). The owner retired it on
2026-11-06. These tests pin both halves of that decision: the URL is now an
unknown `/api/` path (no tile handler runs, no DB is touched), and the
station list the frontend actually uses keeps answering.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.hydro_display import get_hydro_display_session
from tests.api_contract_helpers import _assert_success_envelope, _DataSourceStore

RETIRED_PATH = "/api/v1/tiles/met-stations/{basin_version_id}/{z}/{x}/{y}.pbf"
# The node-27 request that answered 404 MVT_SOURCE_IDENTITY_NOT_FOUND before retirement.
RETIRED_URL = "/api/v1/tiles/met-stations/basins_qhh_vbasins/5/24/12.pbf"
REPO_ROOT = Path(__file__).resolve().parents[1]


class _EmptyStationInventorySession:
    """Answers like a PostGIS session holding no `active_flag = true` station.

    That is the live dg state. A retired route never reaches it; the old handler
    did, and turned the empty result into `MVT_SOURCE_IDENTITY_NOT_FOUND`.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []

    def get_bind(self) -> Any:
        class _Bind:
            class dialect:  # noqa: N801 - mirrors the SQLAlchemy attribute name
                name = "postgresql"

        return _Bind()

    def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append(str(statement))

        class _Result:
            def mappings(self) -> Any:
                return self

            def all(self) -> list[Any]:
                return []

            def first(self) -> None:
                return None

        return _Result()

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_station_tile_route_is_not_registered() -> None:
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert RETIRED_PATH not in paths
    assert not any(path.startswith("/api/v1/tiles/met-stations/") for path in paths)
    # The sibling base-layer tile route stays.
    assert "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf" in paths


def test_station_tile_url_is_an_unknown_path_not_a_missing_identity() -> None:
    session = _EmptyStationInventorySession()
    app.dependency_overrides[get_hydro_display_session] = lambda: session
    try:
        with TestClient(app) as client:
            response = client.get(RETIRED_URL)
    finally:
        app.dependency_overrides.pop(get_hydro_display_session, None)

    assert response.status_code == 404
    # The app's own unknown-`/api/` answer (the SPA catch-all in
    # `apps/api/startup_wiring.py` refuses every unmatched `api/` path), not the
    # retired tile handler's error envelope.
    assert response.json() == {"detail": "Not found"}
    assert "MVT_SOURCE_IDENTITY_NOT_FOUND" not in response.text
    assert response.headers.get("X-Tile-Layer-ID") is None
    assert session.executed == []


def test_station_tile_route_is_absent_from_static_and_runtime_openapi() -> None:
    static_spec = yaml.safe_load((REPO_ROOT / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8"))
    app.openapi_schema = None
    runtime_spec = app.openapi()
    for spec in (static_spec, runtime_spec):
        assert RETIRED_PATH not in spec["paths"]
        operation_ids = {
            operation.get("operationId")
            for path_item in spec["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict)
        }
        assert "getMetStationTile" not in operation_ids
        # The station list the frontend reads is still published.
        assert "/api/v1/met/stations" in spec["paths"]


def test_station_list_still_answers_after_tile_retirement() -> None:
    app.dependency_overrides[get_data_source_store] = lambda: _DataSourceStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/met/stations", params={"basin_version_id": "basins_qhh_vbasins"})
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert [item["station_id"] for item in data["items"]] == ["station_1"]
    assert data["items"][0]["basin_version_id"] == "basins_qhh_vbasins"
