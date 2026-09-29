"""#2627: a basemap cache hit marks the tile as recently served.

The Tianditu cache under `<NHMS_MVT_FILE_CACHE_DIR>/basemap/tianditu/` is pruned
by age, and yd-viewer (which shares the subtree) refreshes on hit; the display
API must too, or a tile served every day would still age out.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.routes import basemap

PNG_TILE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TILE_PATH = "/api/v1/basemap/tianditu/vec/3/5/2"


class _NoUpstream:
    async def get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        raise AssertionError(f"a cache hit must not reach upstream: {url}")


@pytest.fixture
def cached_tile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mvt-cache"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    monkeypatch.setattr(basemap, "_http_client", lambda: _NoUpstream())
    monkeypatch.setattr(basemap, "_throttled_until", {})
    tile = root / "basemap" / "tianditu" / "vec" / "3" / "5" / "2"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(PNG_TILE)
    return tile


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.create_app(), raise_server_exceptions=False)


def _age(path: Path, seconds: float) -> float:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return stamp


def test_a_hit_on_a_tile_older_than_a_day_refreshes_its_mtime(client: TestClient, cached_tile: Path) -> None:
    _age(cached_tile, 3 * 86400)
    before = time.time()

    response = client.get(TILE_PATH)

    assert response.status_code == 200
    assert response.headers["x-tile-cache"] == "hit"
    assert cached_tile.stat().st_mtime >= before - 1


def test_a_hit_on_a_tile_younger_than_a_day_leaves_its_mtime(client: TestClient, cached_tile: Path) -> None:
    stamp = _age(cached_tile, 3600)

    response = client.get(TILE_PATH)

    assert response.headers["x-tile-cache"] == "hit"
    assert cached_tile.stat().st_mtime == pytest.approx(stamp, abs=1e-3)


def test_a_refresh_the_filesystem_refuses_still_serves_the_hit(
    client: TestClient, cached_tile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tile yd-viewer wrote may not be ours to touch; the hit must survive that."""
    stamp = _age(cached_tile, 3 * 86400)

    def refuse(path: object, *args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(basemap.os, "utime", refuse)

    response = client.get(TILE_PATH)

    assert response.status_code == 200
    assert response.content == PNG_TILE
    assert response.headers["x-tile-cache"] == "hit"
    assert cached_tile.stat().st_mtime == pytest.approx(stamp, abs=1e-3)
