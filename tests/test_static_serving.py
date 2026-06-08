"""SPA fallback must serve real built static files (e.g. /geo/*.geojson) instead of
returning index.html for them. The national river basemap fetches /geo/*.geojson at
runtime; if the fallback returned HTML the frontend would silently lose the layer.

Skipped when no frontend build is present (CI backend gate does not build the SPA).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import FRONTEND_DIST_DIR, FRONTEND_INDEX, app

pytestmark = pytest.mark.skipif(not FRONTEND_INDEX.exists(), reason="no frontend dist build present")

client = TestClient(app)


def test_geojson_static_file_is_served_as_json_not_index_html() -> None:
    response = client.get("/geo/national-basin-river.geojson")
    assert response.status_code == 200
    body = response.text.lstrip()
    assert body.startswith("{"), "expected GeoJSON, got non-JSON (likely index.html fallback)"
    assert '"FeatureCollection"' in body
    assert "<!doctype html" not in body.lower()


def test_spa_client_route_falls_back_to_index_html() -> None:
    response = client.get("/hydro-met")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_unknown_api_path_is_404_not_index() -> None:
    assert client.get("/api/v1/definitely-not-a-route").status_code == 404


def test_path_traversal_does_not_escape_dist_root() -> None:
    response = client.get("/geo/../../../../../../etc/passwd")
    # Either rejected or harmlessly served the SPA index — never the real file.
    assert "root:" not in response.text


def test_dist_root_is_resolvable() -> None:
    # Guards the relative_to traversal check against a misconfigured dist path.
    assert FRONTEND_DIST_DIR.resolve().is_dir()
