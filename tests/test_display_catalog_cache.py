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
from fastapi.testclient import TestClient

from apps.api import display_cache
from apps.api.display_cache import (
    clear_display_catalog_cache,
    display_catalog_cached,
    start_display_catalog_warmer,
    stop_display_catalog_warmer,
)
from apps.api.routes import forecast as forecast_routes

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
    url: str | None = None,
) -> SimpleNamespace:
    """构造最小 request 替身。

    `headers`/`scope` 只在显式给出时才设置属性——「既无 `.headers` 也无 `.scope`」
    是 `_force_refresh` 必须容忍的真实形状，默认路径不能悄悄补上它们。

    `url` 同理：没有 `.url` 的 request 不会被登记热 path（`_request_path` 返回 None），
    所以任何要看 `_hot_paths` 的用例都必须显式给一个，否则断言「不在 `_hot_paths` 里」
    是空转的。
    """
    config = SimpleNamespace(display_readonly=display_readonly, display_cache_warm_token=token)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_config=config)))
    if headers is not None:
        request.headers = headers
    if scope is not None:
        request.scope = scope
    if url is not None:
        path, _, query = url.partition("?")
        request.url = SimpleNamespace(path=path, query=query)
    return request


def _warmer_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == WARMER_THREAD_NAME]


def _seed_hot_path() -> None:
    # 时间戳取“此刻”：陈旧的 0.0 落在 DISPLAY_CATALOG_WARM_ACTIVE_WINDOW_SECONDS
    # 活跃窗口之外，预热线程会直接跳过，回放永远不发生。
    display_cache._hot_paths["warm-guard"] = ("/api/v1/runs", time.monotonic(), 1)


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


def test_token_carrying_an_invalid_utf8_byte_is_a_hit_not_an_error() -> None:
    # token 来自 `os.environ`，CPython 按 surrogateescape 解码——`display.env` 里一个非法
    # UTF-8 字节会变成 `\udcXX`。token 侧若按纯 utf-8 encode，比较时抛 UnicodeEncodeError，
    # 每条带该头的目录 GET 变 500。所以两侧都必须 `encode("utf-8", "surrogateescape")`。
    calls: list[int] = []
    token = "ab\udcff"
    request = _request(display_readonly=True, token=token)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(display_readonly=True, headers={WARM_HEADER: "x"}, token=token)
    assert display_cache._force_refresh(warm_request) is False
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


def test_truthy_but_non_true_scope_marker_does_not_force_a_refresh() -> None:
    # 标记必须 `is True`：换成 `bool(...)` 就会把 `scope["state"]` 里任何同名真值
    # （日后某个中间件塞的计数器/字符串）当成进程内预热身份——而那是唯一不需要
    # token 的旁路，放宽它等于把特权还给一个不受本模块控制的键。
    calls: list[int] = []
    request = _request(display_readonly=True, token=None)
    assert display_catalog_cached(request, "k", lambda: "first") == "first"

    warm_request = _request(
        display_readonly=True,
        scope={"type": "http", "state": {WARM_SCOPE_KEY: 1}},
        token=None,
    )
    assert display_cache._force_refresh(warm_request) is False
    value = display_catalog_cached(warm_request, "k", lambda: calls.append(1) or "warmed")

    assert value == "first"
    assert calls == []


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


# --- #2078：准入谓词 / LRU 淘汰 / 登记时机 / 回放上界 ---------------------------


def _junk_request(index: int) -> SimpleNamespace:
    """一条「无界维度」的公网请求：每个 index 一个不同的 key 与不同的 path。"""
    return _request(display_readonly=True, url=f"/api/v1/runs?basin_id=junk-{index}&limit=1")


def test_non_cacheable_keys_neither_evict_the_legit_key_nor_become_replay_targets() -> None:
    """300 条谓词为假的请求不能把合法 key 挤掉，也不能留下任何回放目标。

    改前红：`_store_value` / `_record_hot_path` 到顶整表 `clear()`，第 256 条就把
    刚被命中的合法 key 连同全部热 path 一起清空（node-27 实测 2 ms → 71 ms）。
    """
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")
    assert display_catalog_cached(legit, "k", lambda: {"items": [1]}) == {"items": [1]}
    assert display_catalog_cached(legit, "k", lambda: {"items": ["miss"]}) == {"items": [1]}

    calls: dict[str, int] = {}

    def _empty_page(key: str) -> dict[str, list[Any]]:
        calls[key] = calls.get(key, 0) + 1
        return {"items": []}

    for index in range(300):
        key = f"junk-{index}"
        value = display_catalog_cached(
            _junk_request(index),
            key,
            lambda key=key: _empty_page(key),
            cacheable=lambda page: bool(page["items"]),
        )
        assert value == {"items": []}

    assert "k" in display_cache._store
    assert display_cache._store["k"][1] == {"items": [1]}
    assert display_cache._hot_paths["k"][0] == "/api/v1/runs?limit=1"
    assert [key for key in display_cache._store if key.startswith("junk-")] == []
    assert [key for key in display_cache._hot_paths if key.startswith("junk-")] == []
    # 不缓存 ⇒ 每个 key 恰好跑了一次 loader，且再来一次还会再跑。
    assert calls == {f"junk-{index}": 1 for index in range(300)}
    display_catalog_cached(
        _junk_request(7),
        "junk-7",
        lambda: _empty_page("junk-7"),
        cacheable=lambda page: bool(page["items"]),
    )
    assert calls["junk-7"] == 2


def test_a_cacheable_burst_evicts_one_entry_at_a_time_and_a_hit_saves_the_legit_key() -> None:
    """255 + 命中 + 255：LRU 只淘汰最旧一条，被命中的 key 回到 MRU 端活下来。"""
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")
    assert display_catalog_cached(legit, "k", lambda: "legit") == "legit"

    for index in range(255):
        display_catalog_cached(_junk_request(index), f"first-{index}", lambda index=index: index)
    assert len(display_cache._store) == 256

    assert display_catalog_cached(legit, "k", lambda: "recomputed") == "legit"

    for index in range(255):
        display_catalog_cached(_junk_request(1000 + index), f"second-{index}", lambda index=index: index)

    assert "k" in display_cache._store
    assert display_cache._store["k"][1] == "legit"
    assert len(display_cache._store) == 256
    # 最旧的一条（第一批的第一个）已被淘汰，而不是整表归零。
    assert "first-0" not in display_cache._store


def test_a_cacheable_burst_without_a_hit_evicts_the_legit_key_but_never_empties_the_store() -> None:
    """LRU 的诚实边界：不被访问的合法 key 会被挤掉（一次冷 miss），但表从不为空。"""
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")
    assert display_catalog_cached(legit, "k", lambda: "legit") == "legit"

    for index in range(300):
        display_catalog_cached(_junk_request(index), f"burst-{index}", lambda index=index: index)
        assert len(display_cache._store) >= 1, index

    assert "k" not in display_cache._store
    assert len(display_cache._store) == 256


def test_hot_paths_record_after_the_outcome_with_a_hit_counter() -> None:
    """登记在结果确定之后，且只对可缓存结果：miss 记 1，每次命中 +1，不可缓存永不登记。"""
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")

    display_catalog_cached(legit, "k", lambda: {"items": [1]}, cacheable=lambda page: bool(page["items"]))
    assert display_cache._hot_paths["k"][0] == "/api/v1/runs?limit=1"
    assert display_cache._hot_paths["k"][2] == 1

    for expected in (2, 3, 4):
        display_catalog_cached(legit, "k", lambda: {"items": ["miss"]}, cacheable=lambda page: bool(page["items"]))
        assert display_cache._hot_paths["k"][2] == expected

    empty = _request(display_readonly=True, url="/api/v1/runs?basin_id=nobody&limit=1")
    display_catalog_cached(empty, "empty", lambda: {"items": []}, cacheable=lambda page: bool(page["items"]))
    assert "empty" not in display_cache._hot_paths
    assert "empty" not in display_cache._store


def test_hot_paths_are_lru_bounded_and_never_cleared_as_a_whole() -> None:
    for index in range(300):
        display_catalog_cached(_junk_request(index), f"hot-{index}", lambda index=index: index)
        assert len(display_cache._hot_paths) >= 1, index
    assert len(display_cache._hot_paths) == 256
    assert "hot-0" not in display_cache._hot_paths
    assert display_cache._hot_paths["hot-299"][0] == "/api/v1/runs?basin_id=junk-299&limit=1"


def _marked_warm_scope() -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    async def _fake_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        del receive, send
        seen.append(scope)

    asyncio.run(display_cache._mark_warm_scope(_fake_app)({"type": "http"}, None, None))
    return seen[0]


def test_a_forced_refresh_never_touches_the_hot_path_table() -> None:
    """回放只写 `_store`，不刷新 `_hot_paths`。

    刷新 `last_access` 会让预热线程每 tick 给自己的目标续命，1800 s 活跃窗口永不
    过期；刷新 `hits` 则让排序键由回放自己推高——D4 的代价模型两条都依赖它不发生。
    """
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")
    display_catalog_cached(legit, "k", lambda: {"items": [1]})
    display_catalog_cached(legit, "k", lambda: {"items": ["miss"]})
    before = display_cache._hot_paths["k"]
    assert before[2] == 2

    warm_request = _request(display_readonly=True, scope=_marked_warm_scope(), token=None, url="/api/v1/runs?limit=1")
    assert display_catalog_cached(warm_request, "k", lambda: {"items": [2]}) == {"items": [2]}

    assert display_cache._store["k"][1] == {"items": [2]}
    assert display_cache._hot_paths["k"] == before


def test_a_forced_refresh_yielding_a_non_cacheable_value_forgets_the_key() -> None:
    """回放拿到空结果 ⇒ 忘记该 key：继续回旧值或继续把它当回放目标都是错的。"""
    calls: list[str] = []
    legit = _request(display_readonly=True, url="/api/v1/runs?limit=1")
    cacheable = lambda page: bool(page["items"])  # noqa: E731 - 与路由调用点同形

    assert display_catalog_cached(legit, "k", lambda: {"items": [1]}, cacheable=cacheable) == {"items": [1]}
    assert "k" in display_cache._store
    assert "k" in display_cache._hot_paths

    warm_request = _request(display_readonly=True, scope=_marked_warm_scope(), token=None, url="/api/v1/runs?limit=1")
    replayed = display_catalog_cached(
        warm_request, "k", lambda: calls.append("replay") or {"items": []}, cacheable=cacheable
    )

    assert replayed == {"items": []}
    assert "k" not in display_cache._store
    assert "k" not in display_cache._hot_paths

    fresh = display_catalog_cached(legit, "k", lambda: calls.append("plain") or {"items": [2]}, cacheable=cacheable)
    assert fresh == {"items": [2]}
    assert calls == ["replay", "plain"]


def test_non_display_role_passes_through_without_evaluating_the_predicate() -> None:
    evaluated: list[Any] = []
    calls: list[int] = []
    request = _request(display_readonly=False, url="/api/v1/runs?limit=1")

    for _ in range(2):
        value = display_catalog_cached(
            request,
            "k",
            lambda: calls.append(1) or {"items": []},
            cacheable=lambda page: evaluated.append(page) or bool(page["items"]),
        )
        assert value == {"items": []}

    assert calls == [1, 1]
    assert evaluated == []
    assert display_cache._store == {}
    assert display_cache._hot_paths == {}


def _seed_active_hot_paths() -> str:
    """40 条活跃热 path：1 条 hits=5（最旧），39 条 hits=1（更新）。

    hits=5 那条故意最旧：只按 last_access 排序的实现会把它排到最后，被前 32 条挤掉。
    """
    now = time.monotonic()
    hot_path = "/api/v1/runs?limit=1"
    display_cache._hot_paths["hot"] = (hot_path, now - 100.0, 5)
    for index in range(39):
        display_cache._hot_paths[f"junk-{index}"] = (f"/api/v1/runs?basin_id=junk-{index}", now - index, 1)
    return hot_path


def _run_one_warm_tick(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    called = threading.Event()
    replayed: list[list[str]] = []

    async def fake_replay(app: Any, targets: list[str]) -> None:
        del app
        replayed.append(list(targets))
        called.set()

    monkeypatch.setattr(display_cache, "_replay_targets", fake_replay)
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_INTERVAL_SECONDS", 0.02)
    thread = start_display_catalog_warmer(FastAPI())
    assert thread is not None
    try:
        assert called.wait(2.0) is True
    finally:
        assert stop_display_catalog_warmer() is True
    return replayed


def test_warm_loop_replays_at_most_the_bound_hit_ranked_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """自伤上界：每 tick ≤ 32 条，命中计数高的排第一。

    改前红：`_warm_loop` 把活跃窗口内**全部**热 path 交给 `_replay_targets`
    （node-27 实测 254 条 = 9.2 s 串行 DB 工作 / tick）。
    """
    hot_path = _seed_active_hot_paths()

    replayed = _run_one_warm_tick(monkeypatch)

    assert len(replayed[0]) == display_cache.DISPLAY_CATALOG_WARM_REPLAY_MAX == 32
    assert replayed[0][0] == hot_path


def test_warm_loop_replay_bound_is_the_module_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    hot_path = _seed_active_hot_paths()
    monkeypatch.setattr(display_cache, "DISPLAY_CATALOG_WARM_REPLAY_MAX", 2)

    replayed = _run_one_warm_tick(monkeypatch)

    assert len(replayed[0]) == 2
    assert replayed[0][0] == hot_path


_EMPTY_RUNS_PAGE: dict[str, Any] = {"items": [], "total_count": 0, "limit": 1, "offset": 0}
_ONE_RUN_PAGE: dict[str, Any] = {
    "items": [{"run_id": "run-1", "basin_id": "basin-a", "status": "published"}],
    "total_count": 1,
    "limit": 1,
    "offset": 0,
}


def _runs_app(page: dict[str, Any]) -> tuple[FastAPI, list[dict[str, Any]]]:
    """最小 `/api/v1/runs` 应用：display 角色 + 计数的 store 替身。"""
    calls: list[dict[str, Any]] = []

    class _CountingStore:
        def list_runs(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {key: list(value) if isinstance(value, list) else value for key, value in page.items()}

    app = FastAPI()
    app.state.runtime_config = SimpleNamespace(display_readonly=True, display_cache_warm_token=None)
    app.include_router(forecast_routes.router)
    app.dependency_overrides[forecast_routes.get_forecast_store] = _CountingStore
    return app, calls


def test_an_empty_runs_page_is_served_but_never_cached() -> None:
    app, calls = _runs_app(_EMPTY_RUNS_PAGE)
    params = {"basin_id": "no-such-basin", "limit": 1}
    try:
        with TestClient(app) as client:
            first = client.get("/api/v1/runs", params=params)
            second = client.get("/api/v1/runs", params=params)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "ok"
    assert first.json()["data"] == forecast_routes._paginated_payload(_EMPTY_RUNS_PAGE)
    assert second.json()["data"] == first.json()["data"]
    assert "runs:no-such-basin:None:None:None:1:0" not in display_cache._store
    assert "runs:no-such-basin:None:None:None:1:0" not in display_cache._hot_paths
    assert display_cache._store == {}
    assert display_cache._hot_paths == {}
    # 不缓存 ⇒ 第二次请求必须再落一次 store。
    assert len(calls) == 2


def test_a_runs_page_with_items_is_cached() -> None:
    app, calls = _runs_app(_ONE_RUN_PAGE)
    params = {"basin_id": "basin-a", "limit": 1}
    try:
        with TestClient(app) as client:
            first = client.get("/api/v1/runs", params=params)
            second = client.get("/api/v1/runs", params=params)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert first.json()["data"] == forecast_routes._paginated_payload(_ONE_RUN_PAGE)
    assert second.json()["data"] == first.json()["data"]
    assert len(calls) == 1
    key = "runs:basin-a:None:None:None:1:0"
    assert display_cache._store[key][1] == _ONE_RUN_PAGE
    assert display_cache._hot_paths[key][0] == "/api/v1/runs?basin_id=basin-a&limit=1"
    assert display_cache._hot_paths[key][2] == 2
