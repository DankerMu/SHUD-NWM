"""display_readonly 目录 TTL 缓存（apps/api/display_cache.py）需求场景."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api import display_cache
from apps.api.display_cache import clear_display_catalog_cache, display_catalog_cached


def _request(display_readonly: bool) -> SimpleNamespace:
    config = SimpleNamespace(display_readonly=display_readonly)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_config=config)))


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_display_catalog_cache()
    yield
    clear_display_catalog_cache()


def test_display_role_caches_within_ttl() -> None:
    calls = []
    request = _request(display_readonly=True)
    for _ in range(3):
        value = display_catalog_cached(request, "k", lambda: calls.append(1) or {"n": len(calls)})
    assert calls == [1]
    assert value == {"n": 1}


def test_non_display_roles_pass_through() -> None:
    calls = []
    request = _request(display_readonly=False)
    for _ in range(2):
        display_catalog_cached(request, "k", lambda: calls.append(1) or len(calls))
    assert calls == [1, 1]


def test_missing_runtime_config_passes_through() -> None:
    calls = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    for _ in range(2):
        display_catalog_cached(request, "k", lambda: calls.append(1) or len(calls))
    assert calls == [1, 1]


def test_keys_are_isolated() -> None:
    request = _request(display_readonly=True)
    assert display_catalog_cached(request, "a", lambda: "va") == "va"
    assert display_catalog_cached(request, "b", lambda: "vb") == "vb"
    assert display_catalog_cached(request, "a", lambda: "stale-miss") == "va"


def test_stale_window_serves_stale_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    # 过期但未超 STALE_MAX：回 stale（刷新由预热线程负责），访客不阻塞在慢查询上。
    request = _request(display_readonly=True)
    clock = {"now": 1000.0}
    monkeypatch.setattr(display_cache.time, "monotonic", lambda: clock["now"])
    assert display_catalog_cached(request, "k", lambda: "first") == "first"
    clock["now"] += display_cache.DISPLAY_CATALOG_TTL_SECONDS + 1
    assert display_catalog_cached(request, "k", lambda: "second") == "first"


def test_stale_max_expiry_recomputes(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(display_readonly=True)
    clock = {"now": 1000.0}
    monkeypatch.setattr(display_cache.time, "monotonic", lambda: clock["now"])
    assert display_catalog_cached(request, "k", lambda: "first") == "first"
    clock["now"] += display_cache.DISPLAY_CATALOG_STALE_MAX_SECONDS + 1
    assert display_catalog_cached(request, "k", lambda: "second") == "second"


def test_force_refresh_header_bypasses_cache() -> None:
    request = _request(display_readonly=True)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"
    warm_request = _request(display_readonly=True)
    warm_request.headers = {display_cache.DISPLAY_CACHE_FORCE_REFRESH_HEADER: "refresh"}
    assert display_catalog_cached(warm_request, "k", lambda: "warmed") == "warmed"
    # 预热写回后，普通请求命中新值。
    assert display_catalog_cached(request, "k", lambda: "miss") == "warmed"


def test_loader_errors_are_not_cached() -> None:
    request = _request(display_readonly=True)
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        display_catalog_cached(request, "k", _boom)
    assert display_catalog_cached(request, "k", lambda: "recovered") == "recovered"
    assert calls == [1]
