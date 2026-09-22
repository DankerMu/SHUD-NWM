"""Same-origin Tianditu basemap proxy (`apps/api/routes/basemap.py`).

Upstream is replaced by a recording fake so every scenario asserts both what
the client receives and how many times Tianditu would have been asked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.routes import basemap

PNG_TILE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_TILE = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TILE_PATH = "/api/v1/basemap/tianditu/vec/3/5/2"


class FakeUpstream:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        self.calls.append({"url": url, "params": params})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(*responses: httpx.Response | Exception) -> FakeUpstream:
        fake = FakeUpstream(*responses)
        monkeypatch.setattr(basemap, "_http_client", lambda: fake)
        return fake

    monkeypatch.setattr(basemap, "_throttled_until", 0.0)
    return install


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mvt-cache"
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(root))
    return root


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.create_app(), raise_server_exceptions=False)


def test_first_request_fetches_upstream_with_server_key_and_caches_the_tile(
    client: TestClient, upstream: Any, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHMS_TIANDITU_KEY", "server-side-key")
    fake = upstream(httpx.Response(200, content=PNG_TILE))

    response = client.get(TILE_PATH, headers={"Referer": "https://nwm.ac.cn/", "Cookie": "session=visitor"})

    assert response.status_code == 200
    assert response.content == PNG_TILE
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-tile-cache"] == "miss"
    assert response.headers["cache-control"] == basemap.BASEMAP_CACHE_CONTROL
    [call] = fake.calls
    assert call["url"] == "https://t7.tianditu.gov.cn/DataServer"
    assert call["params"] == {"T": "vec_w", "x": "5", "y": "2", "l": "3", "tk": "server-side-key"}
    assert (cache_root / "basemap" / "tianditu" / "vec" / "3" / "5" / "2").read_bytes() == PNG_TILE
    assert not list((cache_root / "basemap").rglob(".*.tmp"))


def test_upstream_client_identifies_as_a_browser_and_never_forwards_visitor_headers() -> None:
    client = basemap._http_client()

    assert "Chrome/" in client.headers["user-agent"]
    assert "referer" not in client.headers
    assert "cookie" not in client.headers
    assert client.follow_redirects is False


def test_cached_tile_is_served_without_asking_upstream(client: TestClient, upstream: Any, cache_root: Path) -> None:
    fake = upstream(httpx.Response(200, content=JPEG_TILE))
    client.get("/api/v1/basemap/tianditu/img/3/5/2")

    response = client.get("/api/v1/basemap/tianditu/img/3/5/2")

    assert response.status_code == 200
    assert response.content == JPEG_TILE
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-tile-cache"] == "hit"
    assert len(fake.calls) == 1


def test_key_falls_back_to_the_previous_frontend_key_when_env_is_unset(
    client: TestClient, upstream: Any, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NHMS_TIANDITU_KEY", raising=False)
    fake = upstream(httpx.Response(200, content=PNG_TILE))

    assert client.get(TILE_PATH).status_code == 200
    assert fake.calls[0]["params"]["tk"] == basemap.DEFAULT_TIANDITU_KEY


def test_upstream_throttle_is_not_cached_and_opens_a_cooldown(
    client: TestClient, upstream: Any, cache_root: Path
) -> None:
    throttled = httpx.Response(
        429,
        json={"msg": "该tk已限流", "code": 302010},
        headers={"Cache-Control": "max-age=432000"},
    )
    fake = upstream(throttled)

    first = client.get(TILE_PATH)
    second = client.get("/api/v1/basemap/tianditu/cva/3/5/2")

    for response in (first, second):
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "60"
        assert response.json()["error"]["code"] == "BASEMAP_UPSTREAM_THROTTLED"
    # The second miss is answered from the cooldown, not forwarded upstream.
    assert len(fake.calls) == 1
    assert not (cache_root / "basemap").exists() or not any(p.is_file() for p in (cache_root / "basemap").rglob("*"))


def test_upstream_is_asked_again_once_the_cooldown_expires(
    client: TestClient, upstream: Any, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = upstream(httpx.Response(429), httpx.Response(200, content=PNG_TILE))
    assert client.get(TILE_PATH).status_code == 503

    monkeypatch.setattr(basemap, "_throttled_until", 0.0)
    response = client.get(TILE_PATH)

    assert response.status_code == 200
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(403, json={"msg": "非法key", "code": 301001}),
        httpx.Response(200, content=b'{"msg":"not an image"}', headers={"Content-Type": "application/json"}),
        httpx.ConnectTimeout("timed out"),
    ],
    ids=["upstream-403", "non-image-200", "network-error"],
)
def test_upstream_failures_are_502_no_store_and_never_cached(
    client: TestClient, upstream: Any, cache_root: Path, failure: httpx.Response | Exception
) -> None:
    upstream(failure)

    response = client.get(TILE_PATH)

    assert response.status_code == 502
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["error"]["code"] == "BASEMAP_UPSTREAM_UNAVAILABLE"
    assert "tk" not in response.text and basemap.DEFAULT_TIANDITU_KEY not in response.text
    assert not (cache_root / "basemap").exists()


def test_corrupt_cache_file_is_a_miss_and_is_overwritten(client: TestClient, upstream: Any, cache_root: Path) -> None:
    cached = cache_root / "basemap" / "tianditu" / "vec" / "3" / "5" / "2"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"truncated")
    fake = upstream(httpx.Response(200, content=PNG_TILE))

    response = client.get(TILE_PATH)

    assert response.headers["x-tile-cache"] == "miss"
    assert cached.read_bytes() == PNG_TILE
    assert len(fake.calls) == 1


def test_without_a_cache_root_the_tile_is_still_proxied(
    client: TestClient, upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NHMS_MVT_FILE_CACHE_DIR", raising=False)
    fake = upstream(httpx.Response(200, content=PNG_TILE), httpx.Response(200, content=PNG_TILE))

    assert client.get(TILE_PATH).headers["x-tile-cache"] == "miss"
    assert client.get(TILE_PATH).headers["x-tile-cache"] == "miss"
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/v1/basemap/tianditu/osm/3/5/2", 422),
        ("/api/v1/basemap/tianditu/vec/19/0/0", 422),
        ("/api/v1/basemap/tianditu/vec/-1/0/0", 422),
        ("/api/v1/basemap/tianditu/vec/3/8/0", 404),
        ("/api/v1/basemap/tianditu/vec/3/0/8", 404),
    ],
    ids=["unknown-layer", "zoom-above-max", "negative-zoom", "x-outside-grid", "y-outside-grid"],
)
def test_out_of_contract_requests_never_reach_upstream(
    client: TestClient, upstream: Any, cache_root: Path, path: str, status: int
) -> None:
    fake = upstream()

    response = client.get(path)

    assert response.status_code == status
    assert fake.calls == []
