"""display_readonly 目录 TTL 缓存（apps/api/display_cache.py）需求场景."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from apps.api import display_cache
from apps.api.display_cache import (
    clear_display_catalog_cache,
    display_catalog_cached,
    start_display_catalog_warmer,
    stop_display_catalog_warmer,
)

WARMER_THREAD_NAME = "display-catalog-warmer"


def _request(display_readonly: bool) -> SimpleNamespace:
    config = SimpleNamespace(display_readonly=display_readonly)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_config=config)))


def _warmer_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == WARMER_THREAD_NAME]


def _seed_hot_path() -> None:
    # 时间戳取“此刻”：陈旧的 0.0 落在 DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS
    # 活跃窗口之外，预热线程会直接跳过，回放永远不发生。
    display_cache._hot_paths["warm-guard"] = ("/api/v1/runs", time.monotonic())


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


def test_warmer_starts_stops_and_restarts_without_leaking_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # 预热线程必须可停可 join：停不掉就会带着进程全局 time.monotonic 活到后续用例里。
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    app = FastAPI()

    first = start_display_catalog_warmer(app)
    assert first is not None
    assert first.name == WARMER_THREAD_NAME
    assert _warmer_threads() == [first]

    assert stop_display_catalog_warmer() is True
    assert _warmer_threads() == []
    assert display_cache._warmer_started is False
    assert display_cache._warmer_thread is None

    second = start_display_catalog_warmer(app)
    assert second is not None
    assert second is not first
    assert _warmer_threads() == [second]

    assert stop_display_catalog_warmer() is True
    assert _warmer_threads() == []


def test_stop_warmer_when_never_started_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    assert display_cache._warmer_started is False

    assert stop_display_catalog_warmer() is True
    assert _warmer_threads() == []
    assert display_cache._warmer_started is False
    assert display_cache._stop_event.is_set() is False


def test_warm_loop_replays_hot_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # 循环体的活性 oracle：只证明线程还活着不够，回放必须真的被调用。
    called = threading.Event()
    replayed: list[list[str]] = []

    async def fake_replay(app: Any, targets: list[str]) -> None:
        del app
        replayed.append(list(targets))
        called.set()

    monkeypatch.setattr(display_cache, "_replay_targets", fake_replay)
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    _seed_hot_path()

    thread = start_display_catalog_warmer(FastAPI())
    assert thread is not None
    try:
        assert called.wait(2.0) is True
    finally:
        assert stop_display_catalog_warmer() is True
    assert replayed[0] == ["/api/v1/runs"]


def test_stop_warmer_reports_join_timeout_without_resetting_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # 卡在回放里的线程必须响：stop 返回 False 且不重置状态，避免静默放行第二个线程。
    entered = threading.Event()
    release = threading.Event()

    async def blocking_replay(app: Any, targets: list[str]) -> None:
        del app, targets
        entered.set()
        release.wait(0.5)

    monkeypatch.setattr(display_cache, "_replay_targets", blocking_replay)
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    _seed_hot_path()

    thread = start_display_catalog_warmer(FastAPI())
    assert thread is not None
    try:
        # stop 必须落在“线程正卡在回放里”的窗口内，否则第一个 wait 就吸收了停止信号。
        assert entered.wait(2.0) is True
        assert stop_display_catalog_warmer(timeout=0.05) is False
        assert display_cache._warmer_started is True
        assert display_cache._warmer_thread is thread
        assert display_cache._stop_event.is_set() is True
    finally:
        # 必须治好模块状态，否则 conftest 的 autouse teardown 会把这个用例判红。
        release.set()
        assert stop_display_catalog_warmer() is True
    assert _warmer_threads() == []
