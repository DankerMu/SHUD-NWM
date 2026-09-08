"""display_readonly 目录 TTL 缓存（apps/api/display_cache.py）需求场景."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from apps.api import display_cache
from apps.api.display_cache import (
    clear_display_catalog_cache,
    display_catalog_cached,
    start_display_catalog_warmer,
    stop_display_catalog_warmer,
)

WARMER_THREAD_NAME = "display-catalog-warmer"
# 规格字面量（openspec/changes/display-cache-warm-header-trust/specs/…/spec.md）：
# 独立于被测模块的常量，模块改名/改值必须变红而不是跟着漂。
WARM_HEADER = "x-nhms-cache-warm"
WARM_SCOPE_KEY = "nhms_display_cache_warm"


def _request(
    display_readonly: bool,
    *,
    headers: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    token: str | None = None,
) -> SimpleNamespace:
    """构造最小 request 替身。

    `headers`/`scope` 只在显式给出时才设置属性——「既无 `.headers` 也无 `.scope`」
    是 `_force_refresh` 必须容忍的真实形状，默认路径不能悄悄补上它们。
    """
    config = SimpleNamespace(display_readonly=display_readonly, display_cache_warm_token=token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_config=config)))
    if headers is not None:
        request.headers = headers
    if scope is not None:
        request.scope = scope
    return request


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


def test_external_refresh_header_no_longer_bypasses_cache() -> None:
    # 字面值 refresh 曾是特权值；display 侧 GET 无鉴权，公网任意客户端都能拿它把
    # 目录端点打回冷路径。退役后它必须和其它头值一样命中缓存。
    calls: list[int] = []
    request = _request(display_readonly=True)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: "refresh"}, token=None)
    value = display_catalog_cached(warm_request, "k", lambda: calls.append(1) or "warmed")

    assert value == "first"
    assert calls == []


def test_configured_token_forces_recompute_and_stores_the_new_value() -> None:
    request = _request(display_readonly=True, token="abc")
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: "abc"}, token="abc")
    assert display_catalog_cached(warm_request, "k", lambda: "warmed") == "warmed"

    # 写回后普通请求命中新值。
    assert display_catalog_cached(request, "k", lambda: "miss") == "warmed"


def test_wrong_token_value_hits_the_cache() -> None:
    calls: list[int] = []
    request = _request(display_readonly=True, token="abc")
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: "abd"}, token="abc")
    value = display_catalog_cached(warm_request, "k", lambda: calls.append(1) or "warmed")

    assert value == "first"
    assert calls == []


def test_non_ascii_header_value_is_a_hit_not_an_error() -> None:
    # Starlette 按 latin-1 解头，所以 >=0x80 的字节到达时是非 ASCII `str`；
    # `hmac.compare_digest` 对非 ASCII `str` 抛 TypeError。若按 str 比较，
    # token 部署后任意客户端发一个这样的头值就能把每条目录 GET 打成 500。
    calls: list[int] = []
    request = _request(display_readonly=True, token="abc")
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: "abé"}, token="abc")
    value = display_catalog_cached(warm_request, "k", lambda: calls.append(1) or "warmed")

    assert value == "first"
    assert calls == []


@pytest.mark.parametrize("header_value", ["refresh", "abc", "", "true", "1"])
def test_no_header_value_forces_a_refresh_while_the_token_is_unset(header_value: str) -> None:
    calls: list[int] = []
    request = _request(display_readonly=True, token=None)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: header_value}, token=None)
    value = display_catalog_cached(warm_request, "k", lambda: calls.append(1) or "warmed")

    assert value == "first"
    assert calls == []


@pytest.mark.parametrize(
    "request_obj",
    [
        _request(display_readonly=True, token="abc"),
        _request(display_readonly=True, scope={"type": "http"}, token="abc"),
        _request(display_readonly=True, scope={"type": "http", "state": {}}, token="abc"),
        SimpleNamespace(),
        SimpleNamespace(headers={WARM_HEADER: "abc"}),
        SimpleNamespace(scope={}, headers={WARM_HEADER: "abc"}),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), headers={WARM_HEADER: "abc"}),
        _request(display_readonly=True, headers={WARM_HEADER: b"abc"}, token="abc"),
        _request(display_readonly=True, headers={WARM_HEADER: "   "}, token="   "),
    ],
)
def test_force_refresh_is_false_and_never_raises_on_degenerate_requests(request_obj: Any) -> None:
    # 缺 scope / 缺 headers / 缺 app / 缺 runtime_config / 头值非 str / 空 token：
    # 一律 False，且绝不抛——`_force_refresh` 跑在每条目录 GET 的最前面。
    assert display_cache._force_refresh(request_obj) is False


def test_mark_warm_scope_marks_http_scopes_and_passes_others_through() -> None:
    seen: list[dict[str, Any]] = []

    async def _fake_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del receive, send
        seen.append(scope)

    wrapped = display_cache._mark_warm_scope(_fake_app)

    original_http = {"type": "http", "path": "/api/v1/runs"}
    asyncio.run(wrapped(original_http, None, None))
    original_lifespan = {"type": "lifespan"}
    asyncio.run(wrapped(original_lifespan, None, None))

    assert seen[0]["state"][WARM_SCOPE_KEY] is True
    assert seen[0]["path"] == "/api/v1/runs"
    # 原 scope 不被就地改写（httpx 的 ASGITransport scope 无 "state" 键）。
    assert "state" not in original_http
    assert seen[1] is original_lifespan


def test_in_process_warm_scope_mark_forces_recompute_without_any_token() -> None:
    seen: list[dict[str, Any]] = []

    async def _fake_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del receive, send
        seen.append(scope)

    asyncio.run(display_cache._mark_warm_scope(_fake_app)({"type": "http"}, None, None))
    marked_scope = seen[0]

    request = _request(display_readonly=True, token=None)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, scope=marked_scope, token=None)
    assert display_catalog_cached(warm_request, "k", lambda: "warmed") == "warmed"
    assert display_catalog_cached(request, "k", lambda: "miss") == "warmed"


async def test_replay_targets_still_refreshes_a_real_app_without_a_token() -> None:
    # 端到端钉「预热仍生效」：token 未配置时进程内回放必须照旧重算并写回。
    calls: list[int] = []
    app = FastAPI()
    app.state.runtime_config = SimpleNamespace(display_readonly=True, display_cache_warm_token=None)

    @app.get("/api/v1/runs")
    def _runs(request: Request) -> dict[str, int]:
        return display_catalog_cached(request, "e2e", lambda: calls.append(1) or {"n": len(calls)})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://display-cache-test") as client:
        first = await client.get("/api/v1/runs")
    assert first.status_code == 200
    assert first.json() == {"n": 1}
    assert calls == [1]

    await display_cache._replay_targets(app, ["/api/v1/runs"])

    # `_replay_targets` 吞掉每 path 的异常，所以 loader 调用次数才是活性 oracle。
    assert calls == [1, 1]
    assert display_cache._store["e2e"][1] == {"n": 2}


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


def test_start_warmer_rolls_back_state_when_thread_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # 起线程失败（OS 起不来）不能留下“已登记但从未启动”的句柄：那之后每一次 stop 都会在
    # join 处炸 cannot join thread before it is started，重置代码根本走不到，级联不可自愈。
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    app = FastAPI()

    def _failing_start(self: threading.Thread) -> None:
        del self
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patched:
        patched.setattr(threading.Thread, "start", _failing_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            start_display_catalog_warmer(app)

    # 真实 OS 错误只抛一次；模块状态必须自洽，后续 stop 是干净的 no-op。
    assert display_cache._warmer_started is False
    assert display_cache._warmer_thread is None
    assert _warmer_threads() == []
    assert stop_display_catalog_warmer() is True

    # 撤销补丁后仍能真的起一遍并停掉，证明失败路径没有毒化模块状态。
    thread = start_display_catalog_warmer(app)
    assert thread is not None
    try:
        assert _warmer_threads() == [thread]
    finally:
        assert stop_display_catalog_warmer() is True
    assert _warmer_threads() == []
    assert display_cache._warmer_started is False
    assert display_cache._warmer_thread is None


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
        # 必须无限等：带超时的自释放会重开一个窗口——主线程被调度器抢下去时线程已跑完
        # 回放回到 `_stop_event.wait`，stop(timeout=0.05) 就成功了，断言 `is False` 变 flaky。
        # 下面的 finally 无条件 set（断言失败时也走到），且线程是 daemon，无泄漏路径。
        release.wait()

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
